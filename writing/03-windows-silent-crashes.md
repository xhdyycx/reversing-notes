# Windows 底层开发的五个静默崩溃

> 每一个都是 `exit code 0`、没有任何输出，让你以为是自己的逻辑写错了

## 为什么单独讲"静默"

写用户态 Windows 底层代码（shellcode 执行、hook、内存操作）时，最耗时间的不是功能不对，是**程序什么都不说就死了**。

正常的 bug 有栈、有异常码、有 dump。而下面这五类问题的共同特征是：

- 进程退出，`exit code` 是 0
- stdout / stderr 零输出
- 就算挂了 `AddVectoredExceptionHandler` 也进不去
- 调试器里单步又是好的

它们全部来自同一个根源：**内核在替你做事的时候，拒绝的方式是"悄悄不干"，不是"报错"**。

---

## 一、内存 section 的双映射：改保护一定失败

### 现象

想用 section 放 shellcode，正常流程是"map → 写 → 改 RX → 执行"。改保护那一步：

```c
VirtualProtect(base, size, PAGE_EXECUTE_READ, &old);   // 返回 FALSE, GetLastError() == 87
```

换 `NtProtectVirtualMemory` 也一样：

```
0xC000004E  STATUS_INVALID_PAGE_PROTECTION
```

### 原因

**非 `SEC_IMAGE` 的 mapped view，页保护在映射时就固定了，事后改不了。** 这是 section 对象的语义，不是权限问题——用管理员跑也一样。

### 解法：映射两次

```c
NtMapViewOfSection(hSec, NtCurrentProcess(), &base, 0, 0, NULL,
                   &size, ViewUnmap, 0, PAGE_READWRITE);   // 第一次：写
memcpy(base, sc, sc_len);
NtUnmapViewOfSection(NtCurrentProcess(), base);

NtMapViewOfSection(hSec, NtCurrentProcess(), &base2, 0, 0, NULL,
                   &size, ViewUnmap, 0, PAGE_EXECUTE_READ); // 第二次：执行
```

System 会**复用同一个虚拟地址**，全程没有一个 RWX 页。

### 附带的一条

第一次 map 必须是 `PAGE_READWRITE`。直接 map 成 `PAGE_EXECUTE_READ` 然后 `memcpy`，照样 `0xC0000005`——你不能往没有写权限的页里写，这听起来是废话，但错误码不会告诉你这一点。

---

## 二、shellcode 末尾不是 `ret`

### 现象

loader 写完 shellcode，创建线程或直接调用，然后：

```c
WaitForSingleObject(hThread, INFINITE);   // 永远等不到，或者等到之后进程崩
```

### 原因

**shellcode 一般没有 `ret`。** 它跑完就"掉下去"了——掉进后面的零字节或随机数据，触发访问违例。

如果你等的是主线程返回：

```c
((void(*)())sc)();          // 调用它
// 它不会 ret 回来，栈上是垃圾
```

### 解法

两种，按场景选：

```c
// 方式 A：让它自己退出
// shellcode 末尾加：mov eax,0; push eax; call ExitProcess  （或者 Sleep + ExitProcess）

// 方式 B：调用方兜住
AddVectoredExceptionHandler(1, veil_handler);   // 接住 ACCESS_VIOLATION
((void(*)())sc)();
Sleep(4000);
ExitProcess(0);
```

`Sleep(4000) + ExitProcess(0)` 这个组合看着很脏，但它是**唯一稳定**的：既不依赖 shellcode 有返回值，也不依赖栈平衡。

---

## 三、`E9 rel32` 只能跳 ±2GB

### 现象

写 trampoline hook，跳板内存分配在 `0x1FAB...`（低地址），目标函数在 `0x7FFD...`（ntdll）。算出偏移：

```c
int32_t rel = (int32_t)(trampoline - (target + 5));
memcpy(patch, "\xE9", 1);
memcpy(patch + 1, &rel, 4);
```

写入成功，跳转过去，进程静默 `0xC0000005`。

### 原因

`E9` 后面是**有符号 32 位**相对偏移，范围 ±2GB。`0x7FFD... - 0x1FAB...` 差了几十 TB，强转成 `int32_t` 直接截断，落到一个完全无关的地址。

`VirtualAlloc(NULL, ...)` 不保证给你和目标同一区域的地址。

### 解法：显式找落点

```c
for (uintptr_t probe = target - 0x1000000; probe > target - 0x40000000; probe -= 0x10000) {
    void *m = VirtualAlloc((void*)probe, size, MEM_RESERVE|MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    if (!m) continue;
    int64_t delta = (int64_t)m - ((int64_t)target + 5);
    if (delta > INT32_MIN && delta < INT32_MAX) return m;
    VirtualFree(m, 0, MEM_RELEASE);
}
```

从 `target - 0x1000000` 开始逐级往下试，第一个能分配且落在范围内的就是它。实测拿到的偏移 `-16781493`、`-67118789` 都在范围内。

### 还有一条：别拿 `NtProtectVirtualMemory` 当靶子

调试 hook 时顺手 hook 了 `NtProtectVirtualMemory`，然后发现**改不回来了**：

```
0xC0000021  STATUS_ALREADY_COMMITTED
```

因为 `VirtualProtect` 内部就是调它——你 hook 了它，恢复原字节的那次调用也得走你的跳板，形成死锁。

`NtCreateThreadEx` 也不适合当靶子：变参声明 `pSyscallFn(void*, ...)` 时，11 个参数的压栈布局不对，返回 `0xC0000017 STATUS_NO_MEMORY`。必须用强类型函数指针：

```c
NTSTATUS (NTAPI *pNtCreateThreadEx)(PHANDLE, ACCESS_MASK, PVOID, HANDLE,
    PVOID, PVOID, ULONG, SIZE_T, SIZE_T, SIZE_T, PVOID);
```

---

## 四、Halo's Gate：名次差不是 SSN 差

### 现象

经典的 SSN 推算：ntdll 被 hook 后，找相邻的干净 stub，用它们的 SSN 加减名次差。

```python
# 错误做法
sorted_exports = sorted(all_exports, key=lambda e: e.address)
idx = index_of(target)
ssn = sorted_exports[idx-1].ssn + 1
```

实测 hook `NtCreateThreadEx` 后推出 `0x0CA`，真实值是 `0xC9`，**差 1**。

### 原因

ntdll 的导出表里混着三类东西：

1. 真正的 syscall stub（`4C 8B D1 B8 <ssn>`）
2. `Zw*` 别名——和 `Nt*` 指向同一个 stub，但名字不同，会占名次
3. `Rtl*` / 非 syscall 的函数——根本不是 syscall，SSN 无意义

按导出表整体排序取名次差，等于把后两类也算进了距离。

### 解法

**只收干净的 syscall stub，按地址排序，取前后两个邻居：**

```python
def clean_stub(data):
    return data[:4] == b'\x4c\x8b\xd1\xb8'      # mov r10, rcx; mov eax, imm32

stubs = [e for e in exports
         if e.name.startswith('Nt') and clean_stub(e.bytes)]
stubs.sort(key=lambda e: e.address)
```

然后用邻居夹逼：

| 邻居关系 | 含义 |
|---|---|
| `hi.ssn - lo.ssn == 2` | 中间就是目标，唯一解 |
| `== 1` | 目标不是 syscall 函数（两个邻居相邻，中间没东西） |
| `> 2` | 只能给区间估计，别硬猜 |

`== 2` 才是能用的情况。`== 1` 和 `> 2` 都必须明确报错，不能返回一个"差不多"的数——**错的 SSN 比崩溃更难查**，它会调到一个合法但完全不相干的系统调用上。

---

## 五、Module Stomping：从 padding 起线程必死

### 现象

把 shellcode 写进已加载 DLL 的 `.text` 段尾部 padding，然后：

```c
CreateThread(NULL, 0, (LPTHREAD_START_ROUTINE)tgt, NULL, 0, NULL);
```

进程 **21 毫秒内静默退出**，`exit code 0`，stdout 零输出，VEH 不触发。

### 原因

**内核在创建线程时会校验入口地址是否落在映像的合法函数范围内。**

padding 不是函数——它不在任何 `IMAGE_FUNCTION_TABLE` 里，也不符合映像的合法区间定义。于是线程创建失败，而失败的方式是让整个进程退出，不是返回错误。

### 解法：同线程就地接管

```c
((void(*)())tgt)();      // 不创建线程，当前线程直接跳过去
```

实测 8/8 稳定成功。

**为什么这个可行**：当前线程已经在跑，跳到一个可执行页里的任意地址，内核不会做入口校验——这是普通的控制流转移，不是线程创建。

代价是当前线程的控制权交出去了（不会返回），所以要在调用方做好"这是一次单向跳转"的心理准备。

### 怎么找 padding

```c
// 遍历所有已加载模块
for (每个 IMAGE_SCN_MEM_EXECUTE 段) {
    从段尾往前数连续 0x00 / 0xCC 的字节数；
    够 shellcode 长度就用；
}
```

实测 `mshtml.dll` 的 `.text` 尾部有 4076 字节空洞，够放一个不小的 payload。

---

## 一张对照表

| 症状 | 真实原因 | 一句话解法 |
|---|---|---|
| `VirtualProtect` 返回 FALSE / err 87 | section view 的保护映射时固定 | 双映射：RW 写 → unmap → RX map |
| `NtProtectVirtualMemory` 返回 `0xC000004E` | 同上 | 同上 |
| 调用 shellcode 后崩在随机地址 | 它没有 `ret` | Sleep + ExitProcess / VEH 兜底 |
| 跳转后静默 `0xC0000005` | `E9 rel32` 被 ±2GB 截断 | 从 target-0x1000000 起试分配 |
| 改回字节时崩 `0xC0000021` | hook 了 `NtProtectVirtualMemory` 自己 | 别拿它当靶子 |
| `NtCreateThreadEx` 返回 `0xC0000017` | 变参声明导致压栈布局错 | 强类型函数指针 |
| SSN 推出来差 1~2 | 导出表混了 Zw*/Rtl* | 只收干净 stub，按地址夹逼 |
| 起线程后 21ms 静默退出 | padding 不是合法函数入口 | 同线程就地跳转 |

---

## 方法论

这五条的共同结构是：**内核拒绝了你的请求，但拒绝方式不是返回错误码，而是"什么都不做"或者"直接终止"**。

所以排查思路应该是反过来的：

1. **不要先怀疑自己的逻辑**，先确认"这一步到底执行了没有"
2. **在每个关键步骤前后写日志到文件**（不是 stdout——静默退出时 stdout 不会 flush）
3. **返回值必须检查**，尤其是 `NTSTATUS`；很多 API 失败时返回值是"看起来正常"的负数
4. **单步能过、直接跑就崩**，八成是时序或地址空间布局问题，不是逻辑问题

第五点特别重要：调试器会改变地址空间布局。`VirtualAlloc(NULL, ...)` 在调试器下和裸跑下的返回地址可能完全不同——这也是 `E9 rel32` 那类问题"单步好好的，一跑就崩"的原因。

**如果你的代码在调试器里正常、直接跑就死，第一个要怀疑的是地址。**

---

## 最后

这些坑没有一个能靠读文档解决。文档会说"返回 STATUS_INVALID_PAGE_PROTECTION"，但不会说"因为 section 的 view 保护不可变，请用双映射"。

能查到的资料往往只写到"这里要注意"，具体怎么注意得自己撞。这篇的价值就在于：**每个坑都给了实测过的数字和能直接用的写法**。

如果你也踩过类似的静默崩溃，欢迎补充——这类的知识越攒越值钱。
