# 逆向工程 · 笔记与工具

Windows 底层、Python 保护还原、容器镜像分析相关的研究记录和可用工具。

全部内容都是我自己踩过的坑，不是转述文档。

---

## 工具

### [oci-patcher](./oci-patcher)

不改动 Docker 守护进程，直接用 Python 读取、解包和重打包 OCI 镜像 tar。

```bash
python oci_patch.py inspect image.tar          # 看 config / 层 / diffID 是否一致
python oci_patch.py extract image.tar -o ./rootfs   # 按层回放，含 whiteout 语义
python oci_patch.py patch image.tar -o new.tar \
    --add ./overlay --delete usr/share/doc --tag myapp:v2
```

`README` 里写了三个坑：diffID 必须是未压缩 tar 的哈希、whiteout 不能与被删路径同层、绕过 `addfile()` 直接写 `fileobj` 会写坏 tar。每一个都花了几个小时才定位。

产出的镜像在真机上 `docker load` 验证过，40 层 diffID 全部匹配。

---

## 文章

### [在没有 Docker 的机器上改 Docker 镜像](./writing/01-oci-image-without-docker.md)

OCI 镜像的 tar 结构、两条独立的引用链、三个报错信息不说明原因的坑。

### [Pyarmor 保护的 Python 应用：静态还原之后你能看到什么](./writing/02-pyarmor-static-unpack.md)

反编译产物的指纹、怎么判断手上的源码是不是真的（`__pycache__` 绝对路径 / 文件大小 / `PYTHONPATH`）、本地授权校验在反编译代码里的三种典型形态，以及怎么验证改动真的生效。

### [Windows 底层开发的五个静默崩溃](./writing/03-windows-silent-crashes.md)

`exit code 0` 且零输出的五类崩溃：section 双映射、shellcode 没有 `ret`、`E9 rel32` 的 ±2GB、Halo's Gate 名次差、Module Stomping 的线程入口校验。

---

## 一句话

**工具会过时，判断"这里应该有校验但它没有"的直觉不会。**

---

## 许可

工具 MIT。文章 CC BY-NC 4.0。

---

## 相关项目

- [media-cms](https://github.com/xhdyycx/media-cms) — 自托管媒体库协同系统，本文的 OCI 镜像分析与 Pyarmor 还原方法就是在这个项目上练出来的
