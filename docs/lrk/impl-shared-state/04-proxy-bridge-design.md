# 04 proxy 的桥函数划到非可信域：详细设计（已实现，见 05）

> **状态**：设计稿，2026-11-05。**已按 [05](05-proxy-bridge-impl.md) 实现**，实现和本文有几处不同，以 05 §19 为准。
>
> **一句话**：`relay_output` / `relay_input` 及 proxy 的全部 inband 入口划到非可信（Linux）一侧；它们访问 EVL 状态时只能通过少数几个"门"函数；`proxy_ring` 按"谁访问"拆成 EVL 侧、Linux 侧和共享数据三部分。第一阶段不做地址空间隔离，门就是普通函数调用，行为与现在完全一致；但通过把 Linux 侧代码放进单独的源文件、让 EVL 侧结构体对它不可见，由**编译器**来保证边界。

## 0. 背景和范围

讨论中确定了两点：

1. **按约定，只在 inband 运行的 EVL 函数视作 Linux 函数**（doc 13 §5.2）。`relay_output`、`relay_input`、`proxy_write`、`proxy_read`、`proxy_poll`、`proxy_mmap`，以及 `do_proxy_write` 的 inband 分支，都只在 inband 运行，而且调用链的起点都是 Linux（kworker 或 VFS），所以它们都属于非可信域。

> → 见 [doc 14 §12](../14-trust-domain-vs-stage.md) [C-04]
2. **但它们现在直接读写 EVL 的状态**，例如 `relay_output` 里减 `fillsz`、`evl_raise_flag(&ring->oob_wait)`。在可信域和非可信域之间，这些都是没有经过门的跨域访问。

在上一轮的两个方案里（把 `relay_output` 放进可信域，还是放进非可信域），选了**非可信域**：它本质上是在做 Linux I/O，碰到的 EVL 状态只有"交接计数"和"通知"两类，在这里切开，接口最小。

**范围**：输出、输入两个方向都改。两个方向是对称的，只改一边的话，`proxy_ring` 的拆分会做一半。

**不在范围内**：oob 路径（`proxy_oob_write` / `proxy_oob_read` / `proxy_oob_poll`）的逻辑不变；地址空间隔离本身不做（§7 说明隔离以后这套设计怎么用）。

---

## 1. 原本的机制

### 1.1 `proxy_ring` 的字段和访问者

`struct proxy_ring`（`proxy.c:28-44`）。输出方向里 EVL 是生产者、Linux 是消费者；输入方向反过来。

| 字段 | 输出方向（oob 写 → 文件） | 输入方向（文件 → oob 读） | 保护方式 |
|---|---|---|---|
| `bufmem` 指针 | 创建后不变 | 同左 | — |
| `bufmem` 内容 | EVL 写者写入（`do_proxy_write`），`relay_output` 经 `kernel_write` 读出 | `relay_input` 经 `kernel_read` 写入，EVL 读者读出（`do_proxy_read`） | 由 `fillsz` 协议保证读写不重叠 |
| `fillsz` | 写者 `atomic_add_return` 发布；`relay_output` `atomic_sub_return` 消费 | `relay_input` `atomic_add` 发布；读者 `atomic_sub` 消费 | 原子操作 |
| `wroff` | EVL 写者 | `relay_input` | 输出：`ring->lock`；输入：`worker_lock` |
| `rdoff` | `relay_output` | EVL 读者 | 输出：`worker_lock`；输入：`ring->lock` |
| `reserved`、`nesting` | EVL 写者 | EVL 读者 | `ring->lock` |
| `bufsz`、`granularity` | 创建后不变 | 同左 | — |
| `lock`（`hard_spinlock_t`） | EVL 写者（oob 或 inband） | EVL 读者（oob 或 inband） | — |
| `oob_wait`（`evl_flag`） | oob 写者等待；`relay_output` 唤醒 | oob 读者等待；`relay_input` 唤醒 | EVL 等待通道的 hard lock |
| `inband_wait_w` | inband 写者等待；`relay_output` 唤醒；`proxy_poll` 登记 | 不用 | Linux 等待队列 |
| `inband_wait_r` | 不用 | `relay_input` 唤醒；`proxy_poll` 登记 | Linux 等待队列 |
| `relay_work` | 写者在 ring 由空变非空时发起，回调 `relay_output_work` | 读者或 oob poll 发起，回调 `relay_input_work` | `evl_work` 协议 |
| `wq` | Linux workqueue 句柄 | 同左 | — |
| `worker_lock`（`struct mutex`） | `relay_output` | `relay_input` | — |

两个细节：每个 ring 实际上只用 `inband_wait_r` / `inband_wait_w` 中的一个，`init_ring` 却两个都初始化了（`proxy.c:666-667`）；`wroff` / `rdoff` 的归属随方向变化。

`struct proxy_in` 另有三个字段（`proxy.c:50-55`）：

| 字段 | 谁写 | 谁读 |
|---|---|---|
| `reqsz`（原子） | 读者请求数据时加（`proxy_oob_read:449`、`proxy_read:545`、`proxy_oob_poll:489`）；`relay_input` 读完后减（`:278`、`:294`） | `relay_input`、`proxy_oob_poll` |
| `on_eof`（原子） | `relay_input` 读到 EOF 时置位（`:282`） | 读者用 `atomic_cmpxchg` 取走（`:456`、`:549`） |
| `on_error` | `relay_input` 出错时写（`:280`），之后再也不清除 | `do_proxy_read`（`:358-359`） |

`struct evl_proxy` 的 `filp`（目标文件）只有 inband 代码使用；`poll_head` 是 EVL 的 poll 机制。

### 1.2 三把锁

| 锁 | 类型 | 保护的数据 oob 会碰吗 | 结论 |
|---|---|---|---|
| `ring->lock` | `hard_spinlock_t` | 会（`wroff`/`rdoff` 中 EVL 那一个、`reserved`、`nesting`） | oob 锁，正确 |
| `ring->worker_lock` | Linux `struct mutex` | 不会（只有转发者用；用来串行化 kworker 和同步转发的 inband 调用者） | inband 锁，正确，但放在 EVL 对象里 |
| `filp->f_pos_lock` | Linux `struct mutex` | 不会 | Linux 自己的锁 |

### 1.3 输出方向的完整流程

**oob 写者**：

```
proxy_oob_write (:409)
  → do_proxy_write (:170)
       持 ring->lock：检查空间 → 预留（wroff、reserved、nesting++）
       放锁拷贝 raw_copy_from_user → 再持锁
       nesting 归零时 atomic_add_return 发布到 fillsz
       若 ring 由空变非空 → evl_call_inband_from(&ring->relay_work, ring->wq)   [running_inband() 为假]
  ← -EAGAIN 且阻塞模式 → evl_wait_flag(&ring->oob_wait)
```

**inband 写者**：

```
write(2) → vfs_write → proxy_write (:505)
  → do_proxy_write：同上；ring 由空变非空时走 running_inband() 分支（:229-233）：
       放 ring->lock → relay_output(proxy) → return     [在调用者自己的上下文里同步转发]
  ← -EAGAIN 且阻塞模式 → wait_event_interruptible(ring->inband_wait_w, can_write_buffer(ring, count))   [直接读 EVL 的 fillsz/reserved]
```

**转发者 `relay_output`（`:86-154`）逐行看它碰了什么**：

| 行 | 代码 | 碰到的状态 | 归属 |
|---|---|---|---|
| 94 | `mutex_lock(&ring->worker_lock)` | 转发者互斥 | E-ib |
| 96 | `count = atomic_read(&ring->fillsz)` | 已发布的字节数 | 边界 |
| 97 | `rdoff = ring->rdoff` | 消费者下标 | E-ib |
| 99-104 | `filp->f_mode`、`f_pos_lock`、`f_pos` | 目标文件 | Linux |
| 108-112、114 | `ring->bufsz`、`ring->granularity` | 不变量 | EVL（只读） |
| 117 | `kernel_write(filp, ring->bufmem + rdoff, n, ppos)` | 目标文件；读 `bufmem` 内容 | Linux；边界 |
| 119 | `filp->f_pos = *ppos` | 目标文件 | Linux |
| 127 | `count = atomic_sub_return(count, &ring->fillsz)` | 消费 | 边界 |
| 130-132 | `f_pos_lock` 解锁；`ring->rdoff = rdoff` | — | Linux；E-ib |
| 134 | `mutex_unlock(&ring->worker_lock)` | — | E-ib |
| 141-142 | `evl_signal_poll_events(&proxy->poll_head, POLLOUT\|POLLWRNORM)`（仅当 `count == 0`） | EVL poll | EVL |
| 148-150 | `evl_raise_flag(&ring->oob_wait)`；`wake_up(&ring->inband_wait_w)`（`count < bufsz` 时） | EVL 等待对象；Linux 等待队列 | EVL；Linux |
| 152 | 否则 `evl_schedule()` | EVL 调度器 | EVL |

`relay_output` 的循环有一个不变式："只要启动了，就一直转发到 `fillsz` 归零"。写者只在 ring 由空变非空时触发转发，正是依赖这个不变式。

### 1.4 输入方向的完整流程

**oob 读者**：

```
proxy_oob_read (:429)
  → do_proxy_read (:319)：持 ring->lock，avail = fillsz - reserved；够就预留（rdoff、reserved、nesting++）、放锁拷贝、nesting 归零时 atomic_sub 消费；不够就返回 0
  ← 返回 0 且阻塞模式：atomic_add(count, &in->reqsz)（只加一次）→ evl_call_inband_from(&ring->relay_work, ring->wq) → evl_wait_flag(&ring->oob_wait)
     醒来后若 atomic_cmpxchg(&in->on_eof, true, false) 成功则返回 0（EOF）
```

**inband 读者**：

```
read(2) → vfs_read → proxy_read (:526)
  → do_proxy_read：同上
  ← 返回 0 且阻塞模式：atomic_add(count, &in->reqsz)（只加一次）→ relay_input(proxy)   [在调用者上下文里同步转发]
```

**oob poll**：输入 ring 空且 `reqsz == 0` 时，`reqsz` 加一个粒度，`evl_call_inband_from` 预读（`:487-491`）。

**转发者 `relay_input`（`:243-309`）**：

| 行 | 代码 | 碰到的状态 | 归属 |
|---|---|---|---|
| 253 | `mutex_lock(&ring->worker_lock)` | — | E-ib |
| 255-256 | `count = atomic_read(&in->reqsz)`；`wroff = ring->wroff` | 请求量；生产者下标 | 边界；E-ib |
| 258-263 | `f_mode`、`f_pos_lock`、`f_pos` | 目标文件 | Linux |
| 276 | `ret = kernel_read(filp, ring->bufmem + wroff, n, ppos)` | 目标文件；写 `bufmem` | Linux；边界 |
| 277-285 | 出错或 EOF：`atomic_sub(count - len, &in->reqsz)`；`in->on_error = ret` 或 `atomic_set(&in->on_eof, true)` | 请求量、状态 | 边界 |
| 290 | `atomic_add(ret, &ring->fillsz)` | 发布 | 边界 |
| 292 | `wroff = (wroff + n) % ring->bufsz` | — | E-ib |
| 294 | `count = atomic_sub_return(count, &in->reqsz)` | 请求量 | 边界 |
| 296-301 | `f_pos_lock` 解锁；`ring->wroff = wroff`；`worker_lock` 解锁 | — | Linux；E-ib |
| 304-307 | `evl_signal_poll_events(POLLIN)`、`evl_raise_flag(&ring->oob_wait)`、`wake_up(&ring->inband_wait_r)` | EVL；Linux | EVL；Linux |

### 1.5 其他 inband 入口

| 函数 | 碰到的 EVL 状态 |
|---|---|
| `proxy_poll`（`:558`） | 读两个 ring 的 `fillsz`；`poll_wait` 登记到 `inband_wait_w/r` |
| `proxy_mmap`（`:583`） | 只用 `proxy->filp` |
| `proxy_write` 的等待条件 `can_write_buffer` | 读 `fillsz`、`reserved` |

### 1.6 读代码时发现的问题（全部未验证）

整理接口时，必须明确定义"出错时消费多少、发布多少"，于是又把转发循环细读了一遍：

| # | 问题 | 位置 | 后果 | 状态 |
|---|---|---|---|---|
| P1 | `relay_output` 出错时退出外层循环，如果这期间写者又发布了新数据，`fillsz` 不会归零；而写者只在由空变非空时触发转发 | `:106`、`:127` | proxy 输出方向卡死 | 已记在 doc 11 §5 |
| P2 | `relay_output` 的内层循环在 `kernel_write` 出错或返回 0 时提前退出，`rdoff` 只前进了已经处理的分块，剩下的 `len` 字节没有前进；但外层仍然把整个 `count` 从 `fillsz` 里减掉 | `:114-124`、`:127` | `rdoff` 落后于实际消费量。返回 0 时循环还会继续，下一轮从落后的位置开始写，**写出旧数据、漏掉新数据** | 新发现 |
| P3 | `kernel_write` 短写（返回值小于 `n`）被当成写满 `n` 字节 | `:117-119` | 丢数据 | 已有注释承认"lost. Fair enough"（针对出错的情况） |
| P4 | `relay_input` 短读时，`fillsz` 只加了实际读到的 `ret`，`wroff` 却前进了整个 `n` | `:290-292` | ring 中留下 `n - ret` 字节的空洞，读者会把空洞里的旧字节当作数据读出，而真正的尾部数据错位。管道、终端、socket 的短读很常见 | 新发现 |
| P5 | `relay_input` 按 `reqsz` 读，但不检查 ring 的剩余空间。`reqsz` 由多个读者、以及 oob poll 的预读各自累加，可能超过剩余空间 | `:255`、`:266-293` | 覆盖尚未被读走的数据 | 新发现，疑点 |
| P6 | `on_error` 一旦被设置就不会清除 | `:280` | 之后所有读操作都返回这个错误 | 可能是有意的，需确认 |

本设计的第一阶段**不修这些问题**，保持行为不变；但会让它们在门的校验里暴露出来（§4.3），修复留到单独的第三步（§8）。

---

## 2. 设计原则

1. **按访问者归属**：只有转发者和 inband 入口用的状态，划给 Linux 侧；oob 也会碰的状态，留在 EVL 侧；数据本身是共享区。
2. **门要少、要粗、要校验**：Linux 侧对 EVL 状态的每一次修改都经过门；每个门检查参数是否在合法范围内。
3. **Linux 侧出错的后果只限于这个 proxy 的数据**：它可能写错、丢掉、停住这个 proxy 的数据，但不能把 EVL 的计数、等待对象、调度状态改乱。
4. **第一阶段行为不变**：门就是把原来的一两条语句搬进一个函数。
5. **由编译器保证边界**：Linux 侧代码放进单独的源文件，看不到 EVL 侧结构体的定义，想绕过门也编译不过。

---

## 3. 结构拆分

### 3.1 新的三部分

```c
/* ---- EVL 侧（可信），定义只在 proxy.c 里可见 ---- */
struct proxy_ring {
	void *bufmem;                 /* 共享数据区，创建后不变 */
	unsigned int bufsz;           /* 创建后不变 */
	unsigned int granularity;     /* 创建后不变 */
	atomic_t fillsz;              /* 交接计数 */
	unsigned int evl_off;         /* EVL 一侧的下标：输出方向是 wroff，输入方向是 rdoff */
	unsigned int reserved;
	int nesting;
	hard_spinlock_t lock;
	struct evl_flag oob_wait;
	struct evl_work relay_work;
};

struct proxy_in_state {           /* 原 proxy_in 的三个字段，EVL 侧 */
	atomic_t reqsz;
	atomic_t on_eof;
	int on_error;
};

/* ---- Linux 侧（非可信），定义只在 proxy_bridge.c 里可见 ---- */
struct proxy_bridge {             /* 每个 ring 一个 */
	struct proxy_ring *ring;      /* 不透明句柄，只能传给门 */
	void *bufmem;                 /* 共享数据区的 Linux 侧视图 */
	unsigned int bufsz;           /* 快照 */
	unsigned int granularity;     /* 快照 */
	unsigned int relay_off;       /* Linux 一侧的下标：输出方向是 rdoff，输入方向是 wroff */
	struct mutex worker_lock;
	wait_queue_head_t inband_wait;   /* 每个 ring 只需要一个 */
	struct workqueue_struct *wq;
};
```

`struct evl_proxy` 相应地变成：

```c
struct evl_proxy {
	struct proxy_ring out_ring, in_ring;      /* EVL 侧 */
	struct proxy_in_state in;                 /* EVL 侧 */
	struct proxy_bridge *out_br, *in_br;      /* Linux 侧，单独分配 */
	struct file *filp;                        /* Linux 侧使用，见 §3.3 */
	struct evl_element element;
	struct evl_poll_head poll_head;
};
```

### 3.2 旧字段到新位置的对照

| 旧字段 | 新位置 | 说明 |
|---|---|---|
| `bufmem` | `proxy_ring.bufmem` 和 `proxy_bridge.bufmem` | 同一块内存，两侧各存一个指针；隔离后两侧的映射权限不同（§7） |
| `bufsz`、`granularity` | 两侧各存一份 | 第一类（创建后不变）的快照 |
| `fillsz` | `proxy_ring` | Linux 侧只能经门读取和修改 |
| `wroff`（输出）/ `rdoff`（输入） | `proxy_ring.evl_off` | EVL 一侧的下标 |
| `rdoff`（输出）/ `wroff`（输入） | `proxy_bridge.relay_off` | Linux 一侧的下标，只有转发者用 |
| `reserved`、`nesting`、`lock` | `proxy_ring` | 不变 |
| `oob_wait`、`relay_work` | `proxy_ring` | 不变 |
| `inband_wait_r` / `inband_wait_w` | `proxy_bridge.inband_wait` | 合并为一个：输出 ring 只用过 `_w`，输入 ring 只用过 `_r` |
| `wq` | `proxy_bridge` | Linux 对象的句柄 |
| `worker_lock` | `proxy_bridge` | 只有转发者用 |
| `reqsz`、`on_eof`、`on_error` | `proxy_in_state` | 读者在 oob 上要用，留在 EVL 侧；`relay_input` 经门修改 |

### 3.3 关于 `proxy->filp`

`filp` 只被 inband 代码使用（转发、`proxy_poll`、`proxy_mmap`、`dispose` 里的 `fput`）。它在逻辑上属于 Linux 侧，可以挪进 Linux 侧的一个 per-proxy 结构。但 `proxy_factory_build` / `dispose` 是 EVL 代码，要在里面 `fget` / `fput`。第一阶段把 `filp` 留在 `evl_proxy` 里，Linux 侧通过一个不透明的 per-proxy 句柄拿到它；这一点隔离前不影响边界，列为 §10 的待确认项。

---

## 4. 门接口

### 4.1 门的清单

所有门都定义在 EVL 侧（`proxy.c`），在新头文件 `kernel/evl/proxy_gate.h` 里声明，`proxy_bridge.c` 只包含这个头文件。

**输出方向（Linux 侧是消费者）**

| 门 | 签名 | 语义 | 原来的代码 |
|---|---|---|---|
| G1 | `unsigned int proxy_gate_pending(struct proxy_ring *ring)` | 返回已发布、可以消费的字节数 | `atomic_read(&ring->fillsz)`（`:96`） |
| G2 | `unsigned int proxy_gate_consume(struct proxy_ring *ring, unsigned int n)` | 消费 `n` 字节，返回剩余已发布量 | `atomic_sub_return(count, &ring->fillsz)`（`:127`） |
| G3 | `void proxy_gate_out_done(struct proxy_ring *ring, unsigned int remaining)` | 转发结束后通知 oob 写者和 poller | `:141-152` 里除 `wake_up` 外的部分 |

**输入方向（Linux 侧是生产者）**

| 门 | 签名 | 语义 | 原来的代码 |
|---|---|---|---|
| G4 | `unsigned int proxy_gate_requested(struct proxy_ring *ring)` | 返回读者请求的字节数 | `atomic_read(&in->reqsz)`（`:255`） |
| G5 | `void proxy_gate_produce(struct proxy_ring *ring, unsigned int n)` | 发布 `n` 字节 | `atomic_add(ret, &ring->fillsz)`（`:290`） |
| G6 | `unsigned int proxy_gate_fulfil(struct proxy_ring *ring, unsigned int n)` | 从请求量里减去 `n`，返回剩余请求量 | `atomic_sub(...)`、`atomic_sub_return(...)`（`:278`、`:294`） |
| G7 | `void proxy_gate_in_done(struct proxy_ring *ring, int status)` | 记录 EOF 或错误（`status` 为 0 表示正常，`1` 表示 EOF，负数表示错误），并通知 oob 读者和 poller | `:280-282`、`:304-306` |

**inband 写者和读者访问 ring（两个 stage 都可能调用）**

| 门 | 签名 | 语义 | 原来的代码 |
|---|---|---|---|
| G8 | `ssize_t proxy_gate_write(struct proxy_ring *ring, const char __user *u_buf, size_t count, bool *kick)` | 预留、拷贝、发布；ring 由空变非空时 `*kick = true`，**不再自己触发转发** | `do_proxy_write` 去掉 `:228-235` 的触发部分 |
| G9 | `ssize_t proxy_gate_read(struct proxy_ring *ring, struct proxy_in_state *in, char __user *u_buf, size_t count, bool nonblock)` | 预留、拷贝、消费 | `do_proxy_read` |
| G10 | `bool proxy_gate_can_write(struct proxy_ring *ring, size_t count)` | inband 写者的等待条件 | `can_write_buffer` |
| G11 | `unsigned int proxy_gate_fill(struct proxy_ring *ring)` | `proxy_poll` 用的当前填充量 | `atomic_read(&ring->fillsz)`（`:567`、`:573`） |

G1 和 G11 是同一个操作，实现时可以合并，这里分开写只是为了标明调用场景。

### 4.2 G8 怎样消掉 `running_inband()` 分支

现在 `do_proxy_write` 内部用 `running_inband()` 决定"同步转发"还是"挂 irq_work"。拆分后，触发转发这件事交还给调用者，因为每个调用者都知道自己运行在哪个 stage：

```c
/* EVL 侧，oob 入口 */
static ssize_t proxy_oob_write(struct file *filp, const char __user *u_buf, size_t count)
{
	...
	ret = proxy_gate_write(ring, u_buf, count, &kick);
	if (kick)
		evl_call_inband_from(&ring->relay_work, out_br->wq);   /* 只走门 */
	...
}

/* Linux 侧，inband 入口 */
static ssize_t proxy_write(struct file *filp, const char __user *u_buf, size_t count, loff_t *ppos)
{
	...
	ret = proxy_gate_write(br->ring, u_buf, count, &kick);
	if (kick)
		relay_output(br);           /* 直接调用 Linux 侧的转发者 */
	...
}
```

这正是 doc 13 §5.3 要求的"把 inband 代码块抽成函数"，而且做得更彻底：EVL 代码里不再有 inband 分支。

oob 入口要拿 `out_br->wq`，需要读 Linux 侧结构体。为了不越界，`wq` 在创建时存一份到 `proxy_ring` 里，作为第一类快照，因为 `evl_call_inband_from` 需要它。

### 4.3 校验

每个修改 EVL 状态的门都检查参数。第一阶段只**报告**（`EVL_WARN_ON_ONCE`），照常执行原操作，保证行为不变；第三阶段（§8）再改为拒绝。

| 门 | 检查 | 能暴露的问题 |
|---|---|---|
| G2 `consume(n)` | `n` 不超过当前已发布量 | 正常情况下不会触发（`count` 来自 G1 或上一次 G2 的返回值） |
| G5 `produce(n)` | `fillsz + n` 不超过 `bufsz` | **P5** |
| G6 `fulfil(n)` | `n` 不超过当前请求量 | 计数错误 |
| G7 `in_done(status)` | `status` 是 0、1 或合法错误码 | — |

另外，Linux 侧自己可以在调试模式下检查"`relay_off` 前进量等于 consume / produce 的总量"，这样能暴露 **P2**、**P4**。

### 4.4 每个门碰的 EVL 状态和所在 stage

| 门 | 碰的 EVL 状态 | 调用者的 stage | 是否可能睡眠 |
|---|---|---|---|
| G1、G4、G11 | 读 `fillsz` 或 `reqsz` | inband | 否 |
| G2、G5、G6 | 原子修改 `fillsz` 或 `reqsz` | inband | 否 |
| G3、G7 | `poll_head`、`oob_wait`、`on_eof`、`on_error`，经 `run_oob_call` 进入 oob 调度 | inband | 否 |
| G8、G9 | `lock`、`evl_off`、`reserved`、`nesting`、`fillsz`、`bufmem` | oob 或 inband | 否（拷贝用户内存可能缺页，和现在一样） |
| G10 | 读 `fillsz`、`reserved` | inband（`wait_event` 的条件） | 否 |

所有门都不睡眠，都不需要 Linux 的锁，所以对 inband 调用者来说，进入门就像调用一个普通函数。

---

## 5. 改造后的各个函数

### 5.1 文件划分

| 文件 | 域 | 内容 |
|---|---|---|
| `kernel/evl/proxy.c` | EVL（可信） | `struct proxy_ring` 等定义；门 G1–G11；oob 入口 `proxy_oob_write` / `proxy_oob_read` / `proxy_oob_poll`；`proxy_release`；工厂的 build / dispose；`proxy_fops` |
| `kernel/evl/proxy_bridge.c` | Linux（非可信） | `struct proxy_bridge` 定义；`relay_output`、`relay_input` 及其 work 回调；inband 入口 `proxy_write` / `proxy_read` / `proxy_poll` / `proxy_mmap`；bridge 的创建和销毁 |
| `kernel/evl/proxy_gate.h` | 边界 | 不透明类型 `struct proxy_ring;`、`struct proxy_bridge;`；门的声明；bridge 创建、销毁函数的声明 |

`proxy_fops` 要引用 `proxy_bridge.c` 里的 inband 入口，所以这些入口在 `proxy_gate.h` 里声明为非 `static`，这是 Linux → EVL 方向唯一的"反向"依赖，也就是 VFS 调用的入口。

### 5.2 `relay_output`（Linux 侧）

```c
static void relay_output(struct proxy_bridge *br)
{
	unsigned int rdoff, count, len, n;
	struct file *filp = bridge_filp(br);
	loff_t pos, *ppos;
	ssize_t ret = 0;

	mutex_lock(&br->worker_lock);

	count = proxy_gate_pending(br->ring);                          /* G1 */
	rdoff = br->relay_off;

	ppos = NULL;
	if (filp->f_mode & FMODE_ATOMIC_POS) {
		mutex_lock(&filp->f_pos_lock);
		ppos = &pos;
		pos = filp->f_pos;
	}

	while (count > 0 && ret >= 0) {
		len = count;
		do {
			n = (rdoff + len > br->bufsz) ? br->bufsz - rdoff : len;
			if (br->granularity > 0)
				n = min(n, br->granularity);
			ret = kernel_write(filp, br->bufmem + rdoff, n, ppos);
			if (ret >= 0 && ppos)
				filp->f_pos = *ppos;
			len -= n;
			rdoff = (rdoff + n) % br->bufsz;
		} while (len > 0 && ret > 0);
		count = proxy_gate_consume(br->ring, count);              /* G2 */
	}

	if (ppos)
		mutex_unlock(&filp->f_pos_lock);

	br->relay_off = rdoff;

	mutex_unlock(&br->worker_lock);

	proxy_gate_out_done(br->ring, count);                         /* G3：先通知 oob */
	if (count < br->bufsz)
		wake_up(&br->inband_wait);                                /* 再唤醒 inband */
}
```

与原代码逐行对应，只有三处变化：读 `fillsz` 改为 G1，减 `fillsz` 改为 G2，结尾的 EVL 通知改为 G3。原代码"先唤醒 oob 等待者，再唤醒 inband 等待者"的顺序（注释："give precedence to oob waiters"）保持不变。

G3 的实现（EVL 侧）就是原来的 `:141-152` 去掉 `wake_up`：

```c
void proxy_gate_out_done(struct proxy_ring *ring, unsigned int remaining)
{
	struct evl_proxy *proxy = ring_to_proxy(ring);

	if (remaining == 0)
		evl_signal_poll_events(&proxy->poll_head, POLLOUT|POLLWRNORM);

	if (remaining < ring->bufsz)
		evl_raise_flag(&ring->oob_wait);      /* 内含 evl_schedule */
	else
		evl_schedule();
}
```

### 5.3 `relay_input`（Linux 侧）

```c
static void relay_input(struct proxy_bridge *br)
{
	...
	mutex_lock(&br->worker_lock);

	count = proxy_gate_requested(br->ring);                       /* G4 */
	wroff = br->relay_off;
	/* f_pos 处理不变 */

	while (count > 0) {
		len = count;
		do {
			/* 分块计算不变，用 br->bufsz / br->granularity */
			ret = kernel_read(filp, br->bufmem + wroff, n, ppos);
			if (ret <= 0) {
				proxy_gate_fulfil(br->ring, count - len);         /* G6 */
				status = ret ? ret : 1;                           /* 错误或 EOF */
				goto done;
			}
			if (ppos)
				filp->f_pos = *ppos;
			proxy_gate_produce(br->ring, ret);                    /* G5 */
			len -= ret;
			wroff = (wroff + n) % br->bufsz;                      /* 保留 P4 的原样行为 */
		} while (len > 0);
		count = proxy_gate_fulfil(br->ring, count);               /* G6 */
	}
done:
	/* f_pos 解锁不变 */
	br->relay_off = wroff;
	mutex_unlock(&br->worker_lock);

	if (proxy_gate_in_done(br->ring, status))                     /* G7：记录状态、通知 oob；返回是否需要唤醒 inband */
		wake_up(&br->inband_wait);
}
```

原代码只在 `fillsz > 0 || exception` 时通知（`:304`）。G7 内部读 `fillsz` 判断这个条件，并把结果返回给 Linux 侧，用来决定要不要 `wake_up`，这样 Linux 侧不用读 `fillsz`。G7 的签名相应改为 `bool proxy_gate_in_done(struct proxy_ring *ring, int status)`。

### 5.4 inband 入口（Linux 侧）

| 函数 | 改造 |
|---|---|
| `proxy_write` | `do_proxy_write` 换成 G8，`kick` 时直接调 `relay_output`；等待条件 `can_write_buffer` 换成 G10；`wait_event_interruptible` 用 `br->inband_wait` |
| `proxy_read` | `do_proxy_read` 换成 G9；请求数据的 `atomic_add(count, &in->reqsz)` 换成一个门 G12 `proxy_gate_request(ring, n)`；`relay_input` 改为接 `proxy_bridge`；`on_eof` 的 `atomic_cmpxchg` 换成门 G13 `proxy_gate_take_eof(ring)` |
| `proxy_poll` | 读 `fillsz` 换成 G11；`poll_wait` 用 `br->inband_wait`；转发目标的 `poll` 不变 |
| `proxy_mmap` | 只碰 `filp`，不变 |

（G12、G13 是 inband 读者需要、而转发者不需要的两个小门，补在 §4.1 的清单里：`void proxy_gate_request(struct proxy_ring *, unsigned int n)`、`bool proxy_gate_take_eof(struct proxy_ring *)`。oob 读者在 EVL 侧，直接访问 `proxy_in_state`。）

### 5.5 EVL 侧的变化

| 函数 | 改造 |
|---|---|
| `do_proxy_write` | 改名为 G8 `proxy_gate_write`，删除 `running_inband()` 分支，改为返回 `kick` |
| `do_proxy_read` | 改名为 G9 `proxy_gate_read`，`nonblock` 由调用者传入 |
| `proxy_oob_write` | 调 G8，`kick` 时 `evl_call_inband_from` |
| `proxy_oob_read` / `proxy_oob_poll` | 访问 `proxy_in_state` 和 ring，逻辑不变 |
| `init_ring` / `destroy_ring` | 只处理 EVL 侧字段；调用 `proxy_bridge.c` 提供的 `proxy_bridge_create()` / `proxy_bridge_destroy()` 处理 Linux 侧字段（分配 `proxy_bridge`、`alloc_workqueue`、`mutex_init`、`init_waitqueue_head`） |
| `relay_work` 的回调 | 指向 `proxy_bridge.c` 里的 `relay_output_work` / `relay_input_work`。回调从 `evl_work` 找到 bridge，需要 ring 里存一个 bridge 指针（不透明，只用来回传），或者 bridge 自己持有 `evl_work`。倾向前者，见 §10 |

---

## 6. 正确性

| 方面 | 论证 |
|---|---|
| 单线程语义 | 每个门就是原来的一两条语句，调用顺序不变 |
| 并发 | 使用的原子操作、`ring->lock`、`worker_lock` 与原来完全相同，只是位置换了 |
| `relay_output` 的不变式（转发到 `fillsz` 归零） | 循环结构不变，G2 的返回值就是原来的 `atomic_sub_return` |
| 唤醒顺序 | G3 / G7 在 `wake_up` 之前，保持"oob 优先" |
| stage | 门 G1–G7、G10–G13 只被 inband 调用；G8、G9 两个 stage 都可能，和原来的 `do_proxy_write` / `do_proxy_read` 一样 |
| `running_inband()` 分支消失后 | oob 入口不可能走到 `relay_output`；inband 入口总是同步转发，和原来 `running_inband()` 为真时一样 |
| 生命周期 | bridge 跟着 ring 创建和销毁；`destroy_ring` 先 `evl_cancel_work` 再销毁 bridge，保证没有转发者还在用它 |

---

## 7. 隔离以后

第一阶段的门是普通函数调用。将来做地址空间隔离时：

| 部分 | 隔离后的处理 |
|---|---|
| `proxy_ring`、`proxy_in_state`、`poll_head` | 放在 EVL 地址空间，Linux 不可见 |
| `proxy_bridge`、`filp` | 放在 Linux 地址空间 |
| `bufmem` | 共享数据区。输出 ring 对 Linux 只读，输入 ring 对 Linux 可写。数据内容不在保护范围内（保险箱模型只保护 EVL 的状态） |
| 门 G1–G13 | 变成真正的跨域调用，EVL 侧在入口检查 `ring` 句柄是否合法 |
| `relay_work` 回调 | EVL 中继调用 Linux 侧函数，是"可信 → 非可信"的方向，不需要校验 |

**每次转发跨几次域**：输出方向是 1（G1）+ 每轮循环 1（G2）+ 1（G3）；通常只有一两轮。输入方向类似。数据不拷贝。

**非可信一侧能造成什么**：

| 能 | 不能 |
|---|---|
| 写错、丢掉、停住这个 proxy 的数据 | 让 `fillsz` 超出 `[0, bufsz]`（G2、G5 校验） |
| 不通知，让阻塞模式的 oob 读写者一直等 | 改 EVL 的等待对象或调度状态（只能经 G3、G7 走固定的通知路径） |
| | 影响别的 proxy 或别的 EVL 对象 |

设了 `O_NONBLOCK` 的 oob 读写者不会被卡住。阻塞模式下等 Linux 干活，本来就是 proxy 的语义。

---

## 8. 实施步骤

| 步骤 | 内容 | 是否改变行为 |
|---|---|---|
| 第 1 步 | 在 `proxy.c` 内部完成拆分：新结构、门函数、`do_proxy_write` 去掉 inband 分支，Linux 侧函数只通过门访问 EVL 状态。门的校验只报告 | 否 |
| 第 2 步 | 把 Linux 侧函数挪到 `proxy_bridge.c`，EVL 侧结构体只在 `proxy.c` 里定义，由编译器保证边界 | 否 |
| 第 3 步 | 单独修 P1–P5（如果确认是 bug）：定义转发出错时的消费语义、短写短读的处理、生产前检查剩余空间；门的校验从"报告"改为"拒绝" | 是，单独记录 |

第 1、2 步可以合成一次提交，第 3 步单独提交，每个问题一个修改，方便逐个确认。

**改动量估计**：`proxy.c` 约 800 行，第 1、2 步大约移动 300 行、新增 150 行（门、bridge 结构和创建销毁函数）；第 3 步每个问题几行到几十行。

---

## 9. 验证

| 项目 | 方法 |
|---|---|
| 编译 | `build/linux-evl-qemu` 和 `build/linux-evl-full` 两种配置 |
| 边界由编译器保证 | `proxy_bridge.c` 里任何对 `proxy_ring` 字段的直接访问都应编译失败；可以故意加一行验证一次 |
| 静态检查 | `nm` 看 `proxy_bridge.o` 的外部符号：EVL 侧符号只应有门 G1–G13；不应出现 `evl_raise_flag`、`evl_signal_poll_events`、`evl_schedule` 等 |
| 行为不变 | libevl 的 `tests/proxy-echo.c`、`proxy-pipe.c`、`proxy-eventfd.c`、`proxy-poll.c` |
| 门的校验 | 打开 EVL 调试选项跑上面的测试，正常情况下不应有警告；再专门构造 P5 的场景（两个 oob 读者同时请求接近 `bufsz` 的数据），看 G5 是否报告 |
| P2、P4 的复现 | 目标用一个会短读、短写或出错的文件（例如非阻塞管道、写满的管道），观察输出数据是否错位 |

---

## 10. 待确认的问题

1. **两个方向都改，还是先只改输出方向？** 只改输出，`proxy_ring` 就要暂时保留两套写法；两个方向一起改更干净，但改动量翻倍。
2. **第 2 步要不要做？** 也就是是否新建 `proxy_bridge.c`，让编译器来保证边界。不做的话，边界只靠约定。
3. **第 1、2 步是否严格保留 P1–P5 的行为？** 本设计的选择是保留，修复放到第 3 步。
4. **`filp` 放在哪里**（§3.3）：第一阶段留在 `evl_proxy`，还是现在就挪到 Linux 侧的 per-proxy 结构里。
5. **work 回调怎么找到 bridge**（§5.5）：在 ring 里存一个不透明的 bridge 指针，还是由 bridge 持有 `evl_work`。
6. **门的命名**：`proxy_gate_*` 这个前缀是否合适。
7. **P6（`on_error` 不清除）** 是否有意为之。
