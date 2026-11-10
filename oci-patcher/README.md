# oci-patcher

**Edit OCI / Docker image tarballs without a Docker daemon.**

`docker load` and `docker save` are just tar files with a fixed layout. There
are real situations where you cannot use them:

- CI runners and build hosts that only have Python installed
- Air-gapped machines where an image must be patched before it is ever loaded
- Servers where installing a container runtime is not allowed
- Forensics — you want to know what is *actually* inside an image, layer by layer

Doing this by hand is not hard, but there are three traps that each cost about
an hour to diagnose. They are documented below, and this tool handles them.

Pure Python 3.8+, standard library only.

---

## Install

```bash
git clone https://github.com/<you>/oci-patcher
cd oci-patcher
python oci_patch.py --help
```

## Usage

```bash
# what is in this image?
python oci_patch.py inspect image.tar

# list one layer
python oci_patch.py ls image.tar 12

# replay all layers (whiteouts included) into a directory
python oci_patch.py extract image.tar -o ./rootfs

# append a layer: add files, delete paths, retag
python oci_patch.py patch image.tar -o image-new.tar \
    --add ./overlay \
    --delete usr/share/doc,etc/motd \
    --tag myapp:v2 \
    --mtime 1730000000
```

`patch` writes a **new** tar. The source file is never modified.

`--add` maps a local directory onto the container root: `./overlay/etc/foo.conf`
becomes `/etc/foo.conf`.

`--delete` writes an OCI whiteout (`.wh.<name>`), which removes the path from
every lower layer without rewriting them.

`--mtime` pins timestamps so repeated builds produce byte-identical layers.

---

## The three traps

### 1. `diff_id` is the hash of the *uncompressed* tar

Every layer has two hashes and they are easy to confuse:

| field | where | hashes |
|---|---|---|
| `rootfs.diff_ids[i]` | image config | the **uncompressed** tar |
| layer blob filename / `Layers[i]` | manifest, blob store | the **gzipped** tar |

Getting this wrong produces a load failure that names a mismatch but not the
cause:

```
invalid diffID for layer 8: expected "sha256:e5d6b851…", got "sha256:0539704c…"
```

If you just appended a layer and the error points at *an older layer*, the
config edit shifted indices — but nine times out of ten the layer was written
with `sha256(gzip_bytes)` instead of `sha256(tar_bytes)`.

```python
diff_id   = hashlib.sha256(raw_tar_bytes).hexdigest()    # correct
blob_name = hashlib.sha256(gzip_bytes).hexdigest()       # correct
```

### 2. A whiteout must not name a path that the same layer also writes

Whiteouts are empty files named `.wh.<name>` in the *parent* directory:

```
cms/web/.wh.assets        -> delete /cms/web/assets
etc/.wh.hostname          -> delete /etc/hostname
```

Two failure modes:

**(a) Wrong placement.** `X/.wh.__pycache__` inside a directory that the same
layer is also creating yields:

```
failed to mknod('/cms/…/__pycache__/__pycache__', S_IFCHR, 0): not a directory
```

The whiteout path is resolved relative to its own parent, so a whiteout placed
*inside* the directory it is supposed to delete resolves to a child of that
directory.

**(b) Whiteout and target in the same layer.** If the layer contains both
`.wh.foo` and `foo`, the runtime tries to create a character device over a path
that already exists:

```
failed to mknod('/cms/cms-api/pyarmor_runtime_000000', S_IFCHR, 0): file exists
```

The fix is to filter the write set: any archive path that is the target of a
whiteout in the same layer must be dropped before packing. This tool does that
in `build_layer()`.

### 3. Writing to `tf.fileobj` corrupts the archive

`tarfile` tracks a byte offset internally. Bypassing `addfile()` leaves that
counter stale, the end-of-archive marker lands in the wrong place, and the
resulting file cannot be read back — even by `tarfile` itself:

```
tarfile.ReadError: file could not be opened successfully
```

Always hand the payload to `addfile()` with a file-like object:

```python
ti = tarfile.TarInfo("blobs/sha256/…")
ti.size = len(data)
tf.addfile(ti, io.BytesIO(data))     # correct
# tf.fileobj.write(data)             # wrong: offset never advances
```

---

## Verifying a patched image

```bash
# 1. structure is readable again
python oci_patch.py inspect image-new.tar

# 2. no layer reports a diffID mismatch
python oci_patch.py inspect image-new.tar | grep '!diffID'   # expect nothing

# 3. the runtime agrees
docker load -i image-new.tar
```

A patched image produced with `--add` + `--delete` has been loaded and run on a
real host: the added file is present, the whiteout target is gone, and every
`diff_id` matches.

---

## Layout reference

What `docker save` / OCI actually contains:

```
image.tar
├── oci-layout                {"imageLayoutVersion":"1.0.0"}
├── index.json                points at the OCI image manifest blob
├── manifest.json             docker-save wrapper: Config + RepoTags + Layers
└── blobs/sha256/
    ├── <config digest>       JSON: env, entrypoint, rootfs.diff_ids, history
    ├── <manifest digest>     OCI image manifest: config + layers descriptors
    └── <layer digest>        gzipped tar, one per layer
```

`manifest.json` (docker-save) and `index.json` (OCI) are **two different
things** and both must stay consistent. `index.json` → digest of the OCI
manifest blob → digest of the config blob → `diff_ids` of the layers.

When patching, all of these numbers move. This tool regenerates every one of
them rather than trying to patch them in place.

---

## Limitations

- Symlinks inside `--add` directories are not preserved.
- Hardlinks, device nodes and extended attributes in extracted layers are skipped.
- Multi-arch (fat) indexes: only the first manifest is patched.
- No signature handling — patched images are unsigned, which is correct and
  intended (the digest changed).

## License

MIT — see [LICENSE](LICENSE).
