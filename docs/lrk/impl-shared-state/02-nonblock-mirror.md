# 02 proxy 文件的 `O_NONBLOCK` 改为镜像（第二类：Linux 低频写）

## 1. 原本的机制

### 1.1 `O_NONBLOCK` 在 proxy 里决定什么

应用可以把 proxy fd 设成非阻塞。设置后，`oob_write` 遇到 ring 满、`oob_read` 遇到 ring 空时立刻返回，而不是睡在 `oob_wait` 上。proxy 在 5 个地方判断它（改动前的行号）：

| 位置 | 所在函数 | stage | 判断后做什么 |
|---|---|---|---|
| `proxy.c:340` | `do_proxy_read` | **两个 stage 都可能**（被 `proxy_oob_read` 和 `proxy_read` 调用） | 数据不够时，非阻塞模式下改为读出现有的部分 |
| `proxy.c:410` | `proxy_oob_write` | oob | `do_proxy_write` 返回 `-EAGAIN` 时，非阻塞就返回，否则 `evl_wait_flag(&ring->oob_wait)` |
| `proxy.c:435` | `proxy_oob_read` | oob | 没读到数据时，非阻塞就返回，否则请求预读并 `evl_wait_flag` |
| `proxy.c:505` | `proxy_write` | inband | 同 410，但等待用 `wait_event_interruptible` |
| `proxy.c:530` | `proxy_read` | inband | 同 435 的 inband 版本 |

每一处都是 `filp->f_flags & O_NONBLOCK`，`filp` 是 proxy 自己的 `struct file`。

### 1.2 谁会写 `f_flags`

`f_flags` 是 Linux 共享对象 `struct file` 的字段（偏移 0x30）。对一个 EVL 设备文件来说，open 之后只有两条路径会改其中的 `O_NONBLOCK`：

| 路径 | 位置 | 怎么写 |
|---|---|---|
| `fcntl(fd, F_SETFL, ...)` | `setfl()`，`fs/fcntl.c:82-85` | 持 `filp->f_lock`，`filp->f_flags = (arg & SETFL_MASK) \| (filp->f_flags & ~SETFL_MASK)` |
| `ioctl(fd, FIONBIO, &on)` | `ioctl_fionbio()`，`fs/ioctl.c:355-360` | 持 `filp->f_lock`，置位或清除 `O_NONBLOCK` |

其他写 `f_flags` 的地方：`alloc_empty_file`（`fs/file_table.c:191`，open 之前）、`do_dentry_open`（`fs/open.c:968`，open 之前，只清 `O_CREAT` 等）、`fcntl.c:1012/1065`（只改 `FASYNC`），以及 tty、9p 等驱动对它们自己的文件的修改。这些都与 EVL 文件的 `O_NONBLOCK` 无关。

### 1.3 原方案在 oob 上的情况

oob 路径每次都直接读 Linux 共享字段 `f_flags`。按 doc 13 §8.3，这属于"单字读写"：`f_flags` 是一个对齐的 `unsigned int`，读到的不是旧值就是新值，所以原方案是安全的，只是语义上可能晚一步看到 `fcntl` 的结果。

问题在于它让 oob 路径依赖 Linux 的内存：将来做地址空间隔离时，这些读要么经过门，要么让 Linux 的 `struct file` 对 EVL 可见。按 §11.2 的三条判据，`O_NONBLOCK` 适合做镜像：

1. EVL 可以接受读到稍旧的值（原方案本来就是这样）；
2. Linux 里写它的地方只有两处；
3. 只在应用调 `fcntl` / `ioctl(FIONBIO)` 时写，频率极低。

## 2. 修改思路

1. **镜像放在哪里**：`struct evl_file`。它和 `struct file` 一一对应（`evl_open_element` 为每次 open 分配一个 `evl_file_binding`，其中的 `efile` 由 `evl_open_file` 绑定到 `filp->f_oob_state.data`）。同一个 proxy 元素可以被打开多次，每次 open 的 flags 各不相同，所以镜像必须跟着 `struct file` 走，不能放在 `evl_proxy` 里。
2. **初值**：`evl_open_file` 在 open 时从 `filp->f_flags` 取。此时 `f_flags` 已由 `alloc_empty_file` 设好，文件还没有发布给用户。
3. **Linux 推送**：在两个写的地方加一个 Dovetail 钩子 `handle_inband_fflags_change(filp)`。
   - 沿用 Dovetail 已有的弱函数钩子惯例：`kernel/dovetail.c` 里的 `handle_oob_syscall`、`handle_oob_trap_entry` 等都是 `__weak` 默认实现，由 EVL 提供强定义。
   - 调用放在 `f_lock` 临界区**之内**。如果放在放锁之后，两个线程并发 `fcntl` 时可能出现：A 读到自己写的值，B 写入并推送，A 再推送它的旧值，镜像就错了。放在锁内，`f_lock` 天然把镜像的更新串行化。钩子只是一次判断加一次存储，持锁时间增加可以忽略。
   - 不能用现成的 `f_op->check_flags`：它只传 `arg` 不传 `filp`（`fcntl.c:67-68`），不知道该更新哪个文件；而且它在真正赋值之前被调用，后面的 `fasync` 失败的话，镜像就错了。
4. **EVL 实现钩子**：只对 oob 文件（`filp->f_oob_state.data` 非空）生效，`WRITE_ONCE` 写镜像。
5. **oob 读镜像**：proxy 里加 `proxy_nonblock(filp)`，通过 `private_data`（发布后不变）找到 `evl_file_binding`，读 `efile.nonblock`。只替换 oob 可达的三处（340、410、435）；`proxy_write`、`proxy_read` 只在 inband 上运行，按 doc 13 §5.2 不管，保持原样。

## 3. 具体修改

### 3.1 Dovetail：声明钩子和默认实现

`include/dovetail/fs.h`（`CONFIG_DOVETAIL` 分支，第 31-38 行）：

```c
struct file;

/*
 * Called in-band with filp->f_lock held each time the status flags
 * of @filp change after open, so that a companion core may mirror
 * them. Weak default does nothing.
 */
void handle_inband_fflags_change(struct file *filp);
```

`!CONFIG_DOVETAIL` 分支（第 48-51 行）是空的内联函数，所以 fs 代码在不开 Dovetail 时编译结果不变。

`kernel/dovetail.c:86`：

```c
void __weak handle_inband_fflags_change(struct file *filp)
{
}
```

（同时加了 `#include <linux/fs.h>`，让这里能看到声明。）

### 3.2 Linux：两个推送点

`fs/fcntl.c:82-86`（`setfl`）：

```c
	spin_lock(&filp->f_lock);
	filp->f_flags = (arg & SETFL_MASK) | (filp->f_flags & ~SETFL_MASK);
	filp->f_iocb_flags = iocb_flags(filp);
	handle_inband_fflags_change(filp);
	spin_unlock(&filp->f_lock);
```

`fs/ioctl.c:355-361`（`ioctl_fionbio`）：

```c
	spin_lock(&filp->f_lock);
	if (on)
		filp->f_flags |= flag;
	else
		filp->f_flags &= ~flag;
	handle_inband_fflags_change(filp);
	spin_unlock(&filp->f_lock);
```

这就是本项对 Linux 的全部改动：每处一行。

### 3.3 EVL：镜像字段、初值、钩子实现

`include/evl/file.h`：

```c
struct evl_file {
	struct file *filp;
	struct evl_crossing crossing;
	struct list_head watchpoints; /* watchpoint->node.next */
	hard_spinlock_t lock;	      /* guards ->watchpoints */
	bool nonblock;		      /* mirrors O_NONBLOCK in filp->f_flags */
};
...
static inline bool evl_file_nonblock(struct evl_file *efilp)
{
	return READ_ONCE(efilp->nonblock);
}
```

`kernel/evl/file.c`，`evl_open_file` 里设初值（第 65 行），并实现钩子（第 71-82 行）：

```c
	efilp->nonblock = !!(filp->f_flags & O_NONBLOCK);
...
void handle_inband_fflags_change(struct file *filp)
{
	struct evl_file *efilp = filp->f_oob_state.data;

	if (efilp)
		WRITE_ONCE(efilp->nonblock, !!(filp->f_flags & O_NONBLOCK));
}
```

### 3.4 proxy：oob 可达的三处改读镜像

`kernel/evl/proxy.c:75-84`，新增：

```c
/*
 * O_NONBLOCK as seen from code which may run oob: read the EVL
 * mirror instead of filp->f_flags.
 */
static inline bool proxy_nonblock(struct file *filp)
{
	struct evl_file_binding *fbind = filp->private_data;

	return evl_file_nonblock(&fbind->efile);
}
```

替换：

| 位置（改动后） | 原来 | 现在 |
|---|---|---|
| `proxy.c:351`（`do_proxy_read`） | `filp->f_flags & O_NONBLOCK` | `proxy_nonblock(filp)` |
| `proxy.c:421`（`proxy_oob_write`） | 同上 | 同上 |
| `proxy.c:446`（`proxy_oob_read`） | 同上 | 同上 |
| `proxy.c:517`（`proxy_write`）、`proxy.c:542`（`proxy_read`） | 不变 | 不变（只在 inband 运行） |

## 4. 正确性

| 问题 | 论证 |
|---|---|
| 镜像的写者之间会不会冲突 | 推送都在 `f_lock` 内，串行执行；open 时的初值在文件发布之前写 |
| 镜像会不会和 `f_flags` 不一致 | 每次推送都在同一个临界区里、紧跟着 `f_flags` 的修改，读的是刚写好的 `f_flags`。放锁之后，镜像和 `f_flags` 的 `O_NONBLOCK` 位一致 |
| oob 读者会不会读到撕裂的值 | `bool` 单字节，`READ_ONCE` / `WRITE_ONCE` 防止编译器拆分或重复读 |
| 读者看到旧值的窗口 | 推送发生在 `fcntl` 返回之前，所以应用在 `fcntl` 返回后发起的 oob 调用一定看到新值；和 `fcntl` 并发的 oob 调用可能看到旧值，与原方案读 `f_flags` 的语义相同 |
| 钩子会不会碰到已释放的 `evl_file` | `setfl` / `ioctl_fionbio` 通过 fd 持有文件引用，文件的 `release` 不会和它们并发，`f_oob_state.data` 指向的 `evl_file` 在 release 之前一直有效 |
| 非 EVL 文件 | `f_oob_state.data` 为 NULL，钩子什么都不做 |
| 不开 Dovetail 的内核 | 钩子是空内联函数，编译后 fs 代码不变 |

## 5. 验证

**静态检查**（已做）：`f_flags` 偏移 0x30，`O_NONBLOCK` 是 `04000`（0x800），对字节读就是 `0x31` 偏移的 0x8 位。对比改动前后 `proxy.o`：

| 函数 | 改动前 | 改动后 |
|---|---|---|
| `proxy_oob_write` | `mov 0x30(%r14),%eax` | 无 |
| `proxy_oob_read` | `testb $0x8,0x31(%r12)` | 无 |
| `do_proxy_read` | `testb $0x8,0x31(%r8)` | 无 |

改动后这三个函数改为读 `private_data`（偏移 0x20，初始化后不变）再读 `efile.nonblock`（EVL 内存）。

**运行测试**（未做）：写一个小测试，覆盖两个推送点：

1. 创建一个很小的 proxy（例如 `bufsz = 16`），目标是一个不会被读走数据的管道写端；
2. `fcntl(fd, F_SETFL, O_NONBLOCK)` 后，`oob_write` 写满 ring，再写一次应立刻返回 `-EAGAIN`；
3. `ioctl(fd, FIONBIO, &(int){0})` 关掉非阻塞，`oob_write` 应阻塞；另一个线程把管道读空，它应被唤醒并写入成功；
4. 再用 `ioctl(FIONBIO, 1)` 打开，确认又立刻返回 `-EAGAIN`。

另外跑一遍 libevl 已有的 `tests/proxy-*.c`，确认没有回归。

## 6. 局限

- 镜像只覆盖 `O_NONBLOCK` 一位，因为 EVL 的 oob 路径只关心这一位。如果将来要镜像别的 flags（例如 `O_APPEND`），扩展字段和钩子即可。
- proxy 以外还有 9 个 EVL 源文件、共 19 处用到 `O_NONBLOCK`，没有改。镜像已经对所有 EVL 文件生效，其中 oob 可达的只需换成 `evl_file_nonblock()`。
- 找 `evl_file` 仍要读 `filp->private_data`（初始化后不变），见 01 §6。
- 这是第一个"Linux 调用 EVL 提供的接口来推送修改"的例子。它也说明了 §11.2 里"改动量"那条判据的含义：Linux 一侧只改了两行，前提是写操作集中在少数几个函数里。
