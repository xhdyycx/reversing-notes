# 在没有 Docker 的机器上改 Docker 镜像

> 三个把我卡了各一个小时的坑，以及一个能用的工具

## 起因

有台机器要跑一个容器镜像，但镜像是别人给的，里面有几个文件需要替换，还有几个目录要删掉。问题是：这台机器上装不了 Docker。

`docker load` / `docker save` 本质上就是个 tar，理论上用 `tar` 就能改。我一开始也是这么想的，然后连续踩了三个坑，每个都在报错信息里只给了现象、没给原因。

**这篇文章记的三个坑，任何一个都能让你怀疑人生。**

---

## 先搞清结构

`docker save` 出来的东西长这样：

```
image.tar
├── oci-layout                {"imageLayoutVersion":"1.0.0"}
├── index.json                OCI index，指向 image manifest blob
├── manifest.json             docker-save 的壳：Config + RepoTags + Layers
└── blobs/sha256/
    ├── <config>              JSON：env / entrypoint / rootfs.diff_ids / history
    ├── <manifest>            OCI image manifest：config + layers 描述
    └── <layer>               gzip 压缩的 tar，一层一个
```

关键在于这里有**两条独立的引用链**，都得改对：

```
index.json  →  digest(OCI manifest)  →  digest(config)  →  diff_ids[layer]
manifest.json →  Config / Layers（docker-save 自己的写法）
```

很多人（包括我）第一版只改了 `manifest.json`，`index.json` 没动，`docker load` 直接说镜像不存在。因为现代 Docker 读的是 `index.json`。

---

## 坑一：diffID 是**未压缩** tar 的哈希

一层有两个哈希，长得一样，含义完全不同：

| 字段 | 位置 | 哈希的是 |
|---|---|---|
| `rootfs.diff_ids[i]` | config | **未压缩**的 tar |
| blob 文件名 / `Layers[i]` | manifest / blob store | **gzip 压缩后**的 tar |

我第一次用 `sha256(gzip_bytes)` 填了 diff_id，`docker load` 报：

```
invalid diffID for layer 8: expected "sha256:e5d6b851…", got "sha256:0539704c…"
```

这个报错最坑的地方：**它指向的层号是旧的**。你明明在末尾追加了一层，它报第 8 层不匹配。原因是 config 里 diff_ids 的长度和 Layers 数组必须一一对应，你插入一条，后面的索引全错位——但 Docker 校验时还是按位置比，于是报在旧层上。

正确写法：

```python
diff_id   = hashlib.sha256(raw_tar_bytes).hexdigest()   # 未压缩
blob_name = hashlib.sha256(gzip_bytes).hexdigest()      # 压缩后
```

顺带一句：如果要**可复现构建**，`gzip.GzipFile(..., mtime=0)` 必须显式指定，否则 mtime 进 header，每次打包哈希都不一样。

---

## 坑二：whiteout 的名字和位置都有讲究

OCI 的删除语义不是"记一条删除指令"，而是**一个空文件**：

```
cms/web/.wh.assets     →  删除 /cms/web/assets
etc/.wh.hostname       →  删除 /etc/hostname
```

`.wh.` 前缀，放在**父目录**里。

我踩了两个变体：

### (a) 位置错了：whiteout 被放进了它要删的目录内部

我想删所有 `__pycache__`，于是按目录遍历，在每个 `X/__pycache__/` 里写了个 `.wh.__pycache__`。结果：

```
failed to mknod('/cms/…/app_pure_clean/__pycache__/__pycache__', S_IFCHR, 0): not a directory
```

whiteout 的路径是**相对于它自己的父目录**解析的。把它放进 `X/__pycache__/` 里，它表示"删除 `X/__pycache__/__pycache__`"，不是"删除 `X/__pycache__`"。

正确做法：whiteout 写在 `X/` 下，名字是 `.wh.__pycache__`。

### (b) 同一层里既删又写

这个更隐蔽。我同时做了两件事：加白名单 whiteout 删掉一堆垃圾目录，然后把源目录整个打进去。源目录里恰好还留着 `pyarmor_runtime_000000` 和某个 `.bak` 文件——于是同一层里既有 `.wh.pyarmor_runtime_000000`，又有 `pyarmor_runtime_000000` 本体：

```
failed to mknod('/cms/cms-api/pyarmor_runtime_000000', S_IFCHR, 0): file exists
```

运行时先按 whiteout 创建字符设备（whiteout 在实现上就是个 0/0 的字符设备），然后发现同名路径已经存在。

**修法不是删 whiteout，而是在打包前把"被 whiteout 指向的路径"从写入集合里过滤掉：**

```python
WH_TARGETS = {posixpath.join(posixpath.dirname(w), posixpath.basename(w)[4:])
              for w in whiteouts}

def blocked(path):
    return path in WH_TARGETS or any(
        path.startswith(t + "/") for t in WH_TARGETS)
```

`blocked()` 要在三个地方都调用：目录遍历、文件写入、以及任何从源目录自动收集文件的地方。漏一处就白干。

---

## 坑三：别绕过 `addfile()` 直接写 `fileobj`

`tarfile` 内部维护一个字节偏移计数器，`addfile()` 会更新它，然后据此写 end-of-archive 的两个全零块。

我为了图快，对内存里已经有 bytes 的小文件直接：

```python
write_tarinfo(tf, name, len(data))
tf.fileobj.write(data)          # ← 致命
```

tar 文件看着是生成了，大小也对，但**读不回来**：

```
tarfile.ReadError: file could not be opened successfully
```

因为偏移计数器没动，EOF 记录写在了错误的位置，解析器扫到一半就撞上了垃圾。

正确写法：

```python
ti = tarfile.TarInfo(name)
ti.size = len(data)
tf.addfile(ti, io.BytesIO(data))
```

`addfile()` 接受任何有 `read()` 的对象，`BytesIO` 完全够用，性能上没有区别。

---

## 写完之后怎么验证

三步，缺一不可：

```bash
# 1. 结构能读回来
python oci_patch.py inspect image-new.tar

# 2. 没有任何一层报 diffID 不匹配
python oci_patch.py inspect image-new.tar | grep '!diffID'    # 应该没有输出

# 3. 运行时认账
docker load -i image-new.tar
```

第 3 步最实在。我最后是在一台真机上 `docker load` + `docker create` + `docker export`，确认加进去的文件在、whiteout 目标确实没了、40 层的 diff_id 全部匹配，才算收工。

---

## 工具

上面这些逻辑整理成了一个只用标准库的脚本：

```bash
python oci_patch.py inspect image.tar
python oci_patch.py ls image.tar 12
python oci_patch.py extract image.tar -o ./rootfs
python oci_patch.py patch image.tar -o new.tar \
    --add ./overlay --delete usr/share/doc --tag myapp:v2 --mtime 1730000000
```

`extract` 会按层序回放，包括 whiteout 和 `.wh..wh..opq`（opaque 目录），所以拿出来的是**最终文件系统状态**，不是某一层的快照——这一点在分析别人的镜像时特别有用，你能看到哪些文件后来被删掉了。

代码在 [oci-patcher](https://github.com/<you>/oci-patcher)，MIT。

---

## 小结

三个坑的共同点：**报错信息都在描述现象，不在描述原因。**

- diffID 不匹配 → 不是 diffID 算错了，是压缩/未压缩搞混了
- not a directory → 不是目录有问题，是 whiteout 的相对路径语义
- file exists → 不是文件冲突，是同一层里删和写在打架
- 读不回来 → 不是 tar 格式问题，是偏移计数器

OCI 这套格式本身不复杂，复杂的是它把"正确"藏在了几条没有明说的约束里。希望这篇能帮你省掉那三个小时。
