#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
oci-patcher — inspect, extract and repack OCI / Docker image tarballs
without a running Docker daemon.

Why this exists
---------------
There are plenty of situations where `docker` is simply not available:

  * CI runners and build hosts that only have Python
  * Air-gapped machines where you must patch an image before loading it
  * Servers where you are not allowed to install a container runtime
  * Forensics: you need to see what is *actually* inside an image

`docker load` and `docker save` are just tar files with a fixed layout.
Editing them by hand is error prone because of three traps documented in
README.md — every one of them costs an hour to diagnose.

This tool does the boring parts correctly:

  inspect   show manifest / config / layers
  ls        list the members of a single layer
  extract   replay all layers (whiteouts included) into a directory
  patch     append a new layer built from a directory + whiteout deletions

Only the Python standard library is used.

Author: <your handle>
License: MIT
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

CHUNK = 1 << 20
OCI_INDEX_MT = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST_MT = "application/vnd.oci.image.manifest.v1+json"
OCI_LAYER_GZIP_MT = "application/vnd.oci.image.layer.v1.tar+gzip"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blob_name(digest: str) -> str:
    """'sha256:abcd…' -> 'blobs/sha256/abcd…' (accepts bare hex too)."""
    hexdigest = digest.split(":", 1)[-1]
    return f"blobs/sha256/{hexdigest}"


def read_member(tf: tarfile.TarFile, name: str) -> bytes:
    f = tf.extractfile(name)
    if f is None:
        raise KeyError(f"{name} is not a regular member")
    return f.read()


def write_tarinfo(tf: tarfile.TarFile, name: str, size: int,
                  mode: int = 0o644, mtime: int = 0) -> None:
    """Add a regular file header with a stable mtime (reproducible output)."""
    ti = tarfile.TarInfo(name)
    ti.size = size
    ti.mode = mode
    ti.mtime = mtime
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    tf.addfile(ti)


def add_bytes(tf: tarfile.TarFile, name: str, data: bytes,
              mode: int = 0o644, mtime: int = 0) -> None:
    """Add a complete in-memory file.

    Always go through addfile(): writing to tf.fileobj directly leaves the
    archive offset counter stale, and the resulting tar has a broken end-of-
    archive record (tarfile then refuses to read it back).
    """
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = mode
    ti.mtime = mtime
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = "root"
    tf.addfile(ti, io.BytesIO(data))


def copy_stream(src, dst, size: int) -> None:
    remaining = size
    while remaining > 0:
        chunk = src.read(min(CHUNK, remaining))
        if not chunk:
            break
        dst.write(chunk)
        remaining -= len(chunk)


# --------------------------------------------------------------------------
# image access
# --------------------------------------------------------------------------

class Image:
    """Read-only view over an OCI / docker-save tar produced by `docker save`."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._tf = tarfile.open(self.path, "r:")
        names = set(self._tf.getnames())

        # docker save writes manifest.json; pure OCI layouts may only have index.json
        if "manifest.json" in names:
            self.manifest = json.loads(read_member(self._tf, "manifest.json"))
        else:
            self.manifest = None
        if "index.json" in names:
            self.index = json.loads(read_member(self._tf, "index.json"))
        else:
            self.index = None

        if self.manifest is None and self.index is None:
            raise SystemExit("not a docker-save / OCI image tar: no manifest.json, no index.json")

        if self.manifest is None:
            # build a docker-save shaped manifest from the OCI index
            desc = self.index["manifests"][0]
            mf = json.loads(read_member(self._tf, blob_name(desc["digest"])))
            self.manifest = [{
                "Config": blob_name(mf["config"]["digest"]),
                "RepoTags": [],
                "Layers": [blob_name(l["digest"]) for l in mf["layers"]],
            }]

        self.config_path = self.manifest[0]["Config"]
        self.layers = self.manifest[0]["Layers"]
        self.repo_tags = self.manifest[0].get("RepoTags") or []

    # -- config ------------------------------------------------------------

    @property
    def config(self) -> dict:
        return json.loads(read_member(self._tf, self.config_path))

    # -- members -----------------------------------------------------------

    def layer_members(self, index: int) -> tarfile.TarFile:
        """Open layer `index` as a decompressed tar stream."""
        raw = self._tf.extractfile(self.layers[index])
        if raw is None:
            raise SystemExit(f"layer {index} missing")
        gz = gzip.GzipFile(fileobj=raw)
        return tarfile.open(fileobj=gz, mode="r:")

    def close(self) -> None:
        self._tf.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------
# inspect
# --------------------------------------------------------------------------

def cmd_inspect(args) -> int:
    with Image(Path(args.image)) as img:
        cfg = img.config
        print(f"file      : {img.path}")
        print(f"size      : {img.path.stat().st_size:,} bytes")
        print(f"tags      : {', '.join(img.repo_tags) or '(none)'}")
        print(f"arch      : {cfg.get('architecture')}/{cfg.get('os')}")
        print(f"created   : {cfg.get('created', '?')}")
        print(f"entrypoint: {cfg.get('config', {}).get('Entrypoint')}")
        print(f"cmd       : {cfg.get('config', {}).get('Cmd')}")
        ports = (cfg.get("config", {}).get("ExposedPorts") or {})
        if ports:
            print(f"ports     : {', '.join(ports)}")
        vols = (cfg.get("config", {}).get("Volumes") or {})
        if vols:
            print(f"volumes   : {', '.join(vols)}")
        diff_ids = (cfg.get("rootfs", {}).get("diff_ids") or [])
        print(f"layers    : {len(img.layers)}   diff_ids: {len(diff_ids)}")
        print()
        print(f"{'#':>3}  {'compressed':>13}  {'diff_id':>18}  members")
        for i, layer in enumerate(img.layers):
            comp = img._tf.getmember(layer).size
            did = diff_ids[i][7:25] if i < len(diff_ids) else "?"
            try:
                with img.layer_members(i) as lt:
                    n = sum(1 for _ in lt)
            except Exception as exc:                     # noqa: BLE001
                n = f"<{exc.__class__.__name__}>"
            flag = ""
            if i < len(diff_ids):
                flag = "" if diff_ids[i] == "sha256:" + _diff_id_of(img, i) else "  !diffID"
            print(f"{i:>3}  {comp:>13,}  {did:>18}  {n}{flag}")
        return 0


def _diff_id_of(img: Image, index: int) -> str:
    """sha256 of the *uncompressed* tar — computed lazily, cached on the object."""
    cache = getattr(img, "_diffid_cache", None)
    if cache is None:
        cache = img._diffid_cache = {}
    if index in cache:
        return cache[index]
    h = hashlib.sha256()
    raw = img._tf.extractfile(img.layers[index])
    with gzip.GzipFile(fileobj=raw) as gz:
        while True:
            chunk = gz.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    cache[index] = h.hexdigest()
    return cache[index]


# --------------------------------------------------------------------------
# ls
# --------------------------------------------------------------------------

def cmd_ls(args) -> int:
    with Image(Path(args.image)) as img:
        idx = args.layer
        if idx < 0 or idx >= len(img.layers):
            raise SystemExit(f"layer index out of range 0..{len(img.layers) - 1}")
        with img.layer_members(idx) as lt:
            for m in lt:
                kind = "d" if m.isdir() else ("l" if m.issym() else "-")
                print(f"{kind} {m.size:>10,}  {m.name}")
    return 0


# --------------------------------------------------------------------------
# extract
# --------------------------------------------------------------------------

def _safe_join(root: Path, name: str) -> Path:
    name = name.lstrip("/")
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve())):
        raise SystemExit(f"refusing path traversal: {name}")
    return target


def cmd_extract(args) -> int:
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    total = 0
    with Image(Path(args.image)) as img:
        for i in range(len(img.layers)):
            deleted = 0
            with img.layer_members(i) as lt:
                for m in lt:
                    base = posixpath.basename(m.name)
                    parent = posixpath.dirname(m.name)

                    # opaque directory marker: everything below is gone
                    if base == ".wh..wh..opq":
                        victim = _safe_join(root, parent) if parent else root
                        if victim.is_dir():
                            shutil.rmtree(victim, ignore_errors=True)
                        deleted += 1
                        continue

                    # plain whiteout
                    if base.startswith(".wh."):
                        victim = _safe_join(root, posixpath.join(parent, base[4:]))
                        if victim.is_dir() and not victim.is_symlink():
                            shutil.rmtree(victim, ignore_errors=True)
                        elif victim.exists() or victim.is_symlink():
                            victim.unlink()
                        deleted += 1
                        continue

                    target = _safe_join(root, m.name)
                    if m.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if m.issym():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists() or target.is_symlink():
                            target.unlink()
                        os.symlink(m.linkname, target)
                        continue
                    if not m.isfile():
                        continue                      # hardlinks / devices: skipped
                    target.parent.mkdir(parents=True, exist_ok=True)
                    src = lt.extractfile(m)
                    if src is None:
                        continue
                    with open(target, "wb") as out:
                        copy_stream(src, out, m.size)
                    total += 1
            print(f"layer {i:>2}: {deleted:>4} whiteout(s)", file=sys.stderr)
    files = sum(1 for _ in root.rglob("*") if _.is_file())
    print(f"extracted {files:,} file(s) into {root}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# patch
# --------------------------------------------------------------------------

def build_layer(add_dir: Path | None, deletions: list[str],
                mtime: int) -> tuple[bytes, bytes]:
    """Return (uncompressed_tar, gzipped_tar) for the appended layer."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        # whiteouts first, so a reader that applies in order deletes before adding
        for victim in deletions:
            victim = victim.strip("/")
            if not victim:
                continue
            parent, base = posixpath.split(victim)
            wh = posixpath.join(parent, ".wh." + base)
            write_tarinfo(tf, wh, 0, 0o000, mtime)

        if add_dir is not None:
            base_dir = Path(add_dir).resolve()
            paths = sorted(p for p in base_dir.rglob("*"))
            for p in paths:
                rel = p.relative_to(base_dir).as_posix()
                if p.is_dir():
                    ti = tarfile.TarInfo(rel)
                    ti.type = tarfile.DIRTYPE
                    ti.mode = 0o755
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = "root"
                    ti.mtime = mtime
                    tf.addfile(ti)
                elif p.is_file():
                    mode = 0o755 if os.access(p, os.X_OK) else 0o644
                    with open(p, "rb") as fh:
                        ti = tarfile.TarInfo(rel)
                        ti.size = p.stat().st_size
                        ti.mode = mode
                        ti.mtime = mtime
                        ti.uid = ti.gid = 0
                        ti.uname = ti.gname = "root"
                        tf.addfile(ti, fh)
                # symlinks are intentionally copied as files/ignored; add if you need them
    raw = buf.getvalue()
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0, compresslevel=6) as g:
        g.write(raw)
    return raw, gz.getvalue()


def cmd_patch(args) -> int:
    src = Path(args.image)
    dst = Path(args.out)
    if not dst.suffix:
        raise SystemExit("output must look like a file, e.g. -o image-new.tar")

    deletions = []
    for d in (args.delete or []):
        deletions.extend(x for x in d.split(",") if x.strip())

    mtime = args.mtime or int(time.time())

    with Image(src) as img:
        cfg = img.config
        manifest = json.loads(json.dumps(img.manifest))   # deep copy
        index = json.loads(json.dumps(img.index)) if img.index else None

        print(f"source layers: {len(img.layers)}", file=sys.stderr)
        print(f"whiteouts    : {len(deletions)}", file=sys.stderr)

        raw_layer, gz_layer = build_layer(Path(args.add) if args.add else None,
                                          deletions, mtime)
        raw_digest = sha256_bytes(raw_layer)      # <-- diff_id, uncompressed
        gz_digest = sha256_bytes(gz_layer)        # <-- blob digest, compressed
        print(f"new layer    : diff_id sha256:{raw_digest[:16]}…  "
              f"blob sha256:{gz_digest[:16]}…", file=sys.stderr)

        # --- update config ------------------------------------------------
        rootfs = cfg.setdefault("rootfs", {})
        diff_ids = rootfs.setdefault("diff_ids", [])
        diff_ids.append("sha256:" + raw_digest)
        history = cfg.setdefault("history", [])
        history.append({
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)),
            "created_by": "oci-patcher",
            "comment": "appended by oci-patcher",
        })
        cfg_bytes = json.dumps(cfg, separators=(",", ":")).encode()
        cfg_digest = sha256_bytes(cfg_bytes)

        # --- update docker-save manifest ----------------------------------
        manifest[0]["Config"] = blob_name(cfg_digest)
        manifest[0]["Layers"] = list(img.layers) + [blob_name(gz_digest)]
        if args.tag:
            manifest[0]["RepoTags"] = [args.tag]
        manifest_bytes = json.dumps(manifest, indent=2).encode()

        # --- update OCI index ---------------------------------------------
        if index is not None:
            mf_blob_digest = sha256_bytes(manifest_bytes)
            # OCI image manifest (not the docker-save wrapper) has to be rebuilt too
            # layer blob digests are already encoded in the blob file names
            layers_desc = [
                {"mediaType": OCI_LAYER_GZIP_MT,
                 "digest": "sha256:" + l.rsplit("/", 1)[-1],
                 "size": img._tf.getmember(l).size}
                for l in img.layers
            ]
            layers_desc.append({"mediaType": OCI_LAYER_GZIP_MT,
                                "digest": "sha256:" + gz_digest,
                                "size": len(gz_layer)})
            oci_manifest = {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST_MT,
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                           "digest": "sha256:" + cfg_digest,
                           "size": len(cfg_bytes)},
                "layers": layers_desc,
            }
            oci_manifest_bytes = json.dumps(oci_manifest, separators=(",", ":")).encode()
            oci_manifest_digest = sha256_bytes(oci_manifest_bytes)
        else:
            oci_manifest_bytes = None
            oci_manifest_digest = None

        # --- write the new tar --------------------------------------------
        written = 0
        with tarfile.open(dst, "w", format=tarfile.PAX_FORMAT) as out:
            # copy every original blob except the superseded config
            for m in img._tf.getmembers():
                if not m.name.startswith("blobs/"):
                    continue                       # layout files are rewritten below
                if m.name == img.config_path:
                    continue                       # config is regenerated
                f = img._tf.extractfile(m)
                if f is None:
                    continue
                out.addfile(m, f)
                written += 1

            # new layer blob
            add_bytes(out, blob_name(gz_digest), gz_layer)

            # config blob
            add_bytes(out, blob_name(cfg_digest), cfg_bytes)

            # OCI image manifest blob (only when the source had an index.json)
            if oci_manifest_bytes is not None:
                add_bytes(out, blob_name(oci_manifest_digest), oci_manifest_bytes)

            # layout files last
            add_bytes(out, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
            add_bytes(out, "manifest.json", manifest_bytes)

            if index is not None:
                index["manifests"][0]["digest"] = "sha256:" + oci_manifest_digest
                index["manifests"][0]["size"] = len(oci_manifest_bytes)
                if args.tag:
                    repo, _, tag = args.tag.partition(":")
                    index["manifests"][0].setdefault("annotations", {})[
                        "io.containerd.image.name"] = f"docker.io/library/{repo}:{tag or 'latest'}"
                    index["manifests"][0]["annotations"][
                        "org.opencontainers.image.ref.name"] = tag or "latest"
                idx_bytes = json.dumps(index, separators=(",", ":")).encode()
                add_bytes(out, "index.json", idx_bytes)

    size = dst.stat().st_size
    print(f"\nwritten: {dst}  {size:,} bytes", file=sys.stderr)
    print(f"SHA256 {sha256_of_file(dst)}")
    return 0


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="oci_patch",
        description="inspect / extract / repack OCI image tarballs without Docker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="show config and layers")
    p.add_argument("image")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("ls", help="list the members of one layer")
    p.add_argument("image")
    p.add_argument("layer", type=int)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("extract", help="replay all layers into a directory")
    p.add_argument("image")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("patch", help="append a layer of files and/or whiteouts")
    p.add_argument("image")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--add", help="directory whose contents become the new layer")
    p.add_argument("--delete", action="append",
                   help="container path to delete (repeatable, comma-separated ok)")
    p.add_argument("--tag", help="override RepoTags, e.g. myimage:v2")
    p.add_argument("--mtime", type=int, help="fixed mtime for reproducible layers")
    p.set_defaults(func=cmd_patch)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
