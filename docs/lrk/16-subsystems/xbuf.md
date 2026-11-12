# xbuf.c：跨 stage 的双向缓冲区

> 源码：`kernel/evl/xbuf.c`（770 行）、`include/evl/xbuf.h`、`include/uapi/evl/xbuf-abi.h`。分析结果：`tools/trust-closure/out/subsys/xbuf.txt`。

## 1. 功能

xbuf（cross-buffer）是 **EVL 线程和普通 Linux 线程之间**的双向字节流通道。实时一侧在 oob 上读写，非实时一侧在 inband 上用普通的 `read`/`write`/`poll`。实时一侧永远不用等 Linux。

```
                     inbound ring（ibnd）
 EVL 线程  ──oob_write()──▶ [ ring ] ──read()──▶  Linux 线程
 (oob)     ◀──oob_read()─── [ ring ] ◀──write()──  (inband)
                     outbound ring（obnd）
```

方向是从 Linux 一侧看的：inbound 是"流进 Linux"，outbound 是"从 Linux 流出"。

使用者：
- 用户态：libevl 的 `evl_create_xbuf()` 创建，两边各自打开同一个 `/dev/evl/xbuf/<name>`；
- 内核：`evl_write_xbuf()` / `evl_read_xbuf()`。例如 `drivers/evl/latmus.c:154`，延迟测试驱动在 oob 定时器回调里，用 `evl_write_xbuf(..., O_NONBLOCK)` 把测量结果发给用户态工具。

## 2. 原理

### 2.1 一个环形缓冲区加"预留—拷贝—发布"协议

两个 ring 结构相同，读写都分三步：

1. **持锁预留**：确认空间（或数据）够，把 `wroff`（或 `rdoff`）向前推进 `len`，`wrrsvd += len`，`wrpending++`；
2. **放锁拷贝**：拷贝用户内存可能缺页，耗时不确定，不能持着关硬件中断的锁做；
3. **持锁收尾**：`--wrpending`，**最后一个**完成拷贝的人把累计的预留量一次性发布到 `fillsz`（`fillsz += wrrsvd`，`wrrsvd = 0`），然后通知对端。

这样多个写者（或读者）可以并发拷贝，而对端只会看到完整发布的数据。读者算可读量时会减去 `rdrsvd`，也就是正在被别的读者拷走的部分。

两条语义：
- **写不拆分**：空间放不下整条消息时，写者要么阻塞，要么（`O_NONBLOCK`）返回 `-EAGAIN`，不会只写一部分。
- **读默认凑满**：阻塞模式下等到够 `len` 字节；`O_NONBLOCK` 下有多少读多少。所以消息边界不保证，它是字节流。

### 2.2 两个方向，不同的等待和通知方式

每个 ring 一边在 oob、一边在 inband，所以等待方式和通知方式都要按 stage 选：

| ring | 读者 | 读者怎么等 | 写者 | 写者怎么等 | 锁 |
|---|---|---|---|---|---|
| inbound | Linux 线程（inband） | Linux 等待队列 `ibnd.i_event`：`wait_event_interruptible(fillsz >= len)` | EVL 线程（oob） | EVL 标志 `ibnd.o_event`：`evl_wait_flag` | 独立的 hard lock `ibnd.lock` |
| outbound | EVL 线程（oob） | EVL 等待队列 `obnd.i_event`：`evl_wait_event(fillsz >= len)` | Linux 线程（inband） | Linux 等待队列 `obnd.o_event`：`wait_event_interruptible` | 借用 `obnd.i_event` 的 `wchan.lock` |

跨 stage 的通知：

| 谁通知谁 | 怎么做 | 原因 |
|---|---|---|
| oob 写者 → inband 读者 | `irq_work_queue(&ibnd.irq_work)`，回到 inband 后由 `resume_inband_reader` 调 `wake_up` | oob 上不能调 `wake_up` |
| inband 读者 → oob 写者 | `evl_raise_flag_nosched(&ibnd.o_event)`，最后 `evl_schedule()` | inband 可以直接操作 EVL 对象 |
| oob 读者 → inband 写者 | `irq_work_queue(&obnd.irq_work)` → `resume_inband_writer` → `wake_up` | 同第一行 |
| inband 写者 → oob 读者 | `evl_flush_wait_locked(&obnd.i_event)`，在 ring 锁内完成 | outbound 的 ring 锁就是这个等待队列的锁，所以能在同一个临界区里发布数据并唤醒 |

这些差别全部封装在 `xbuf_ring` 的 6 个函数指针里（`lock`/`unlock`/`wait_input`/`signal_input`/`wait_output`/`signal_output`），`do_xbuf_read`/`do_xbuf_write` 是两个方向共用的通用代码。

### 2.3 防死锁的短读

如果缓冲区比"读者要的量 + 写者要写的量"还小，读者等数据、写者等空间，就会互相等死。所以读者在准备睡眠之前，先看对端有没有写者在等：

- inbound（读者在 inband）：`evl_wait_flag_head(&ibnd->o_event)`，看有没有 oob 写者在等；
- outbound（读者在 oob）：`wq_has_sleeper(&obnd->o_event)`，看有没有 inband 写者在等。

如果有，就返回 `-EAGAIN`，让 `do_xbuf_read` 退回成短读：有多少读多少。

## 3. 数据结构

### 3.1 `struct xbuf_ring`：一个方向的环形缓冲区

| 字段 | 含义 | 谁写 | 保护 |
|---|---|---|---|
| `bufmem` | 缓冲区内存（`kzalloc`） | build | 创建后不变 |
| `bufsz` | 大小，0 表示这个方向不存在（读写返回 `-ENOBUFS`） | build（来自用户的 `attrs.i_bufsz/o_bufsz`） | 不变 |
| `fillsz` | **已发布**的字节数 | 读者、写者的收尾阶段 | ring 锁；inband 等待条件里无锁读（单字） |
| `rdoff` / `rdrsvd` / `rdpending` | 读下标、正在被拷走的字节数、正在拷贝的读者数 | 读者 | ring 锁 |
| `wroff` / `wrrsvd` / `wrpending` | 写下标、正在拷入的字节数、正在拷贝的写者数 | 写者 | ring 锁 |
| `lock` / `unlock` | 加锁、解锁函数 | build | 不变 |
| `wait_input(ring, len, avail)` | 读者等数据，返回 `-EAGAIN` 表示要求短读 | build | 不变 |
| `signal_input(ring, sigpoll)` | 写者发布后通知读者；`sigpoll` 表示由空变非空 | build | 不变 |
| `wait_output(ring, len)` | 写者等空间 | build | 不变 |
| `signal_output(ring, sigpoll)` | 读者消费后通知写者；`sigpoll` 表示由满变不满 | build | 不变 |

### 3.2 `struct xbuf_inbound`（oob_write → read）

| 字段 | 类型 | 含义 |
|---|---|---|
| `i_event` | Linux `wait_queue_head` | inband 读者在这里等 |
| `o_event` | `evl_flag` | oob 写者在这里等 |
| `irq_work` | Linux `irq_work` | oob 写者唤醒 inband 读者的跳板，回调 `resume_inband_reader` |
| `ring` | `xbuf_ring` | — |
| `lock` | `hard_spinlock_t` | 这个 ring 的锁 |

### 3.3 `struct xbuf_outbound`（write → oob_read）

| 字段 | 类型 | 含义 |
|---|---|---|
| `i_event` | `evl_wait_queue` | oob 读者在这里等；它的 `wchan.lock` 同时是这个 ring 的锁 |
| `o_event` | Linux `wait_queue_head` | inband 写者在这里等 |
| `irq_work` | Linux `irq_work` | oob 读者唤醒 inband 写者的跳板，回调 `resume_inband_writer` |
| `ring` | `xbuf_ring` | — |

### 3.4 `struct evl_xbuf` 和两个描述符

| 结构 | 字段 | 含义 |
|---|---|---|
| `evl_xbuf` | `element` | EVL element（设备节点、名字、引用计数） |
| | `ibnd`、`obnd` | 两个方向 |
| | `poll_head` | oob poll 的等待点 |
| `xbuf_rdesc`（栈上） | `buf`、`buf_ptr`、`count` | 目标缓冲区、当前位置、要读的量 |
| | `xfer` | 拷贝函数：`write_to_user`（`raw_copy_to_user`）或 `write_to_kernel`（`memcpy`） |
| `xbuf_wdesc`（栈上） | 同上 | 拷贝函数：`read_from_user` 或 `read_from_kernel` |

描述符让同一套 `do_xbuf_*` 既服务用户态（系统调用），也服务内核调用者（`evl_*_xbuf`）。

## 4. 函数语义

| 函数 | stage | 语义 |
|---|---|---|
| `do_xbuf_read(ring, rd, f_flags)` | 两者 | 通用读：§2.1 的三步；不够时按 `O_NONBLOCK` 返回 `-EAGAIN` 或部分数据，否则调 `ring->wait_input`；收尾时发布消费量并调 `ring->signal_output`；最后 `evl_schedule()` |
| `do_xbuf_write(ring, wd, f_flags)` | 两者 | 通用写：放不下整条消息就返回 `-EAGAIN` 或调 `ring->wait_output`；拷贝失败时把没拷到的部分清零（别的写者可能已经在后面写了数据，没法回滚）；收尾时发布并调 `ring->signal_input` |
| `inbound_lock/unlock` | 两者 | 加解 `ibnd.lock` |
| `inbound_wait_input` | inband | inband 读者等：先检查有没有 oob 写者在等（防死锁），再 `wait_event_interruptible(i_event, fillsz >= len)` |
| `inbound_signal_input` | oob | 通知 inband 读者：`irq_work_queue` |
| `resume_inband_reader` | inband（irq_work 回调） | `wake_up(&ibnd.i_event)` |
| `inbound_wait_output` | oob | oob 写者等：`evl_wait_flag(&ibnd.o_event)` |
| `inbound_signal_output` | inband | 通知 oob 写者：必要时发 POLLOUT 事件，`evl_raise_flag_nosched` |
| `outbound_lock/unlock` | 两者 | 加解 `obnd.i_event.wchan.lock` |
| `outbound_wait_input` | oob | oob 读者等：先 `wq_has_sleeper(&obnd->o_event)`（防死锁），再 `evl_wait_event(i_event, fillsz >= len)` |
| `outbound_signal_input` | inband（持 ring 锁） | 通知 oob 读者：必要时发 POLLIN 事件，`evl_flush_wait_locked` |
| `outbound_wait_output` | inband | inband 写者等：`wait_event_interruptible(o_event, fillsz + len <= bufsz)` |
| `outbound_signal_output` | oob | 通知 inband 写者：`irq_work_queue` |
| `resume_inband_writer` | inband | `wake_up(&obnd.o_event)` |
| `xbuf_read` / `xbuf_write` | inband（`read(2)` / `write(2)`） | 在 inbound 上读 / 在 outbound 上写，用 `filp->f_flags` |
| `xbuf_oob_read` / `xbuf_oob_write` | oob | 在 outbound 上读 / 在 inbound 上写，用 `filp->f_flags` |
| `xbuf_poll` | inband | 两个 Linux 等待队列都登记；inbound 有数据 → POLLIN，outbound 不满 → POLLOUT |
| `xbuf_oob_poll` | oob | 登记 `poll_head`；outbound 有数据 → POLLIN，inbound 不满 → POLLOUT |
| `xbuf_ioctl` / `xbuf_oob_ioctl` | — | 都返回 `-ENOTTY` |
| `xbuf_release` | inband | 用 `EVL_T_RMID` 踢醒两个 oob 等待者，再释放 element |
| `evl_get_xbuf(efd, &efilp)` | 内核 API | 按 fd 找 xbuf，**用 `efilp->filp->f_op == &xbuf_fops` 判断类型** |
| `evl_put_xbuf` | 内核 API | 放引用 |
| `evl_read_xbuf` / `evl_write_xbuf` | 内核 API，两者 | 内核调用者读写；阻塞模式下如果当前不能阻塞（`evl_cannot_block()`：在中断里或在 inband 上），返回 `-EPERM` |
| `xbuf_factory_build` | inband | 校验大小（至少一个方向非 0，都不超过 2^30），分配对象和两块缓冲区，初始化两个方向的锁、等待对象、irq_work 和 6 个函数指针 |
| `xbuf_factory_dispose` | inband | 销毁等待对象和 element，释放缓冲区，`kfree_rcu` |
| `rings_show` | inband（sysfs） | 输出两个 ring 的 `fillsz`、`bufsz`（无锁读） |

## 5. 可信域划分

按 [README 的规则 1–10](README.md#划分规则)。字段级的原始结果在 `tools/trust-closure/out/subsys/xbuf.txt`（种子见 `cfg/xbuf.json`），下面是在它基础上人工推导的类型级结论。

### 5.1 代码：入口和调用链（规则 1、2）

| 入口 | 调用者 | 进入的函数 | 参数 | 结论 |
|---|---|---|---|---|
| oob 系统调用 | EVL 核心分派（经 `filp->f_op->oob_*`） | `xbuf_oob_read/write/poll/ioctl` | `filp` 来自 EVL 的 `evl_file`；用户缓冲区和长度是用户输入 | 可信。但分派时读的 `f_op` 是 Linux 对象的字段，属于 EVL 核心的 M4 问题 |
| 内核 oob 调用 | latmus 的测量线程（oob） | `evl_write_xbuf`、`evl_read_xbuf`、`evl_get_xbuf` | `buf` 是内核指针，`efd` 按值传入（来自用户，经 `evl_get_file` 在 EVL 的 fd 表里查） | 可信；**latmus 的这条路径也因此是可信代码** |
| inband 系统调用 | VFS（Linux） | `xbuf_read/write/poll/ioctl` | `filp` 是 Linux 给的指针，`element_of` 读 `private_data` 找 xbuf | 可信代码（写 ring 状态），但入口在 Linux：找 xbuf 的方式要改成在 EVL 的表里核对（README 结论 3） |
| 关闭 | VFS | `xbuf_release` | 同上 | 可信（刷新 EVL 等待队列） |
| element 释放 | Linux 的 irq_work（`do_put_element_irq`） | `xbuf_factory_dispose` | `work` 指针来自 Linux，经两次 `container_of` 得到 xbuf | **可信代码，入口在 Linux，拿的是 Linux 给的指针**。这是 element 生命周期的共性问题（README 结论 1） |
| inband 唤醒 | Linux 的 irq_work | `resume_inband_reader/writer` | `work` 指针来自 Linux | 只对 Linux 等待队列调 `wake_up`，不写可信数据 → **不可信代码**，规则 8 移出等待队列后留在不可信侧 |
| sysfs | Linux 的 sysfs | `rings_show` | `dev` 来自 Linux | 只读可信数据，用于显示 → 只读门 |

规则 2 的检查：可信代码调用的 Linux 函数有 `wait_event_interruptible`（`inbound_wait_input`、`outbound_wait_output`）、`wake_up`、`irq_work_queue`、`raw_copy_to/from_user`。

- 在 Linux 等待队列上睡眠：醒来后持 hard lock 重查 `fillsz`，Linux 只能影响"什么时候"。**符合规则 2**。
- 用户内存拷贝：返回值只决定"拷了多少"和是否出错，写进 ring 的长度由可信代码按 `count` 和 `bufsz` 算出；拷贝失败时用 0 填充（§6 第 3 点）。**符合**。
- `irq_work_queue` 在 oob 上调用：oob 上调用有状态的 Linux 服务，**不符合**，见 5.4。

### 5.2 分配单元（规则 4、5、6）

| 分配单元 | 分配点 | 结论 |
|---|---|---|
| `struct evl_xbuf` | `xbuf_factory_build`：`kzalloc(sizeof(*xbuf))` | **可信**。oob 读写它的 ring 状态 |
| 两块 ring 缓冲区 `bufmem` | `kzalloc(attrs.i_bufsz)`、`kzalloc(attrs.o_bufsz)` | **可以放在不可信域**（见下） |
| `evl_xbuf_attrs` | 栈，`copy_from_user` | 用户输入，按值，规则 3 |
| `xbuf_rdesc` / `xbuf_wdesc` | 栈，由可信的调用者填写（含 `xfer` 函数指针和缓冲区指针） | 地址只传给本文件的函数，不发布 → 规则 3 |

`evl_xbuf` 里嵌入的成员按规则 6 跟随它：`element`、两个 `xbuf_ring`（含函数指针 `lock`、`wait_input` 等，由 build 设置，之后不变）、`ibnd.o_event`（`evl_flag`）、`ibnd.lock`、`obnd.i_event`（`evl_wait_queue`）、`poll_head`。**例外是 5.4 的 Linux 成员。**

**为什么 `bufmem` 可以不可信。** 你之前担心过"ring 的内容是由不可信域填写的"。按规则，可以这样看：

- EVL 只**搬运**缓冲区里的字节，从不解释它们。决定读写多少、读写到哪里的是 `fillsz`、`rdoff`、`wroff`、`bufsz`，这些都在 `evl_xbuf` 里，是可信的。
- 每个方向总有一端是 Linux：inbound 是 oob 写、Linux 读；outbound 是 Linux 写、oob 读。所以数据本身要么是给 Linux 的，要么来自 Linux，**数据的可信程度不会因为放在可信域而提高**。outbound 方向的实时任务本来就必须校验收到的内容。
- 所以 `bufmem` 放在不可信域，Linux 改写它也只能改变数据内容，不能让 EVL 越界或者算错状态。这就是 doc 14 规则矩阵里"可信代码把不可信数据当不透明数据使用"。

这是一个**可选**的设计。放在可信域也没有问题，只是会增大可信区域。如果某个用户（比如 latmus）两端都是可信代码，就应该放在可信域。

### 5.3 `evl_xbuf` 类型的写者（规则 4）

| 写者 | 写什么 |
|---|---|
| `do_xbuf_read`、`do_xbuf_write` | ring 的 `fillsz`、`rdoff`、`rdrsvd`、`rdpending`、`wroff`、`wrrsvd`、`wrpending` |
| `xbuf_factory_build` | 全部字段（初始化） |
| `xbuf_factory_dispose`、`xbuf_release` | 销毁、刷新等待队列 |
| `inbound/outbound_lock/unlock/wait/signal` | 锁、`evl_flag`、`evl_wait_queue`、poll head |
| EVL 核心：`flag`、`wait`、`poll`、`factory`（element） | 嵌入的 EVL 成员，对所有实例都一样 |
| **Linux：等待队列代码、irq_work 核心、RCU、cdev** | 嵌入的 Linux 成员，见 5.4。按规则必须消除 |

### 5.4 嵌入的 Linux 成员（规则 8）

| 成员 | 谁写 | 现在怎么用 | 改法 |
|---|---|---|---|
| `ibnd.i_event`（`wait_queue_head`） | Linux 等待代码 | inband 读者在上面睡；`resume_inband_reader` 唤醒 | 单独分配在不可信域，可信侧只持指针、只交给 `wait_event`/`wake_up` |
| `obnd.o_event`（`wait_queue_head`） | 同上 | inband 写者在上面睡；**oob 读者用 `wq_has_sleeper` 偷看它**（`outbound_wait_input`） | 同上；偷看改成 EVL 自己的"inband 等待写者数"，由 `outbound_wait_output` 增减 |
| `ibnd.irq_work`、`obnd.irq_work` | Linux irq_work 核心 | oob 上 `irq_work_queue`，推迟到 inband 去 `wake_up` | 换成 EVL 自己的 inband 中继（撤回补丁 03 的思路） |
| `element.rcu`、`element.cdev`、`element.irq_work` 等 | RCU、字符设备核心、irq_work | 所有 element 共有 | EVL 核心拆分 `evl_element`（README 结论 1） |

### 5.5 依赖的 Linux 分配单元（规则 9）

| Linux 对象 | 字段 | 在哪里读 | 截断方式 |
|---|---|---|---|
| `struct file` | `f_flags`（`O_NONBLOCK`） | 所有读写路径，含 oob | 镜像到 EVL 自己的文件对象（M3） |
| `struct file` | `private_data` | `element_of`，含 oob | 从 `evl_file` 找对象（M4） |
| `struct file` | `f_op` | `evl_get_xbuf` 判断文件类型，**在 oob 上**（latmus） | 在 `evl_file` 里记录类型 |

### 5.6 小结，以及和 proxy 的对比

| 问题 | 结论 |
|---|---|
| 可信代码 | xbuf.c 里除 `resume_inband_reader/writer` 和 `rings_show` 外的全部函数；调用链上的 latmus 测量路径 |
| 可信分配单元 | `evl_xbuf` |
| 可选不可信 | 两块 `bufmem` |
| 需要改的地方 | 两个等待队列头移出、两个 irq_work 换掉（规则 8）；`f_flags`、`private_data`、`f_op` 截断（规则 9）；inband 入口和释放入口核对指针（规则 1）。后两类是所有 EVL 设备的共性问题 |

和 proxy 的结构几乎一样：ring、两套等待机制、irq_work 跳板。区别在于**非实时一侧的进度由谁推进**：

| | proxy | xbuf |
|---|---|---|
| 非实时一侧怎么推进 `fillsz` | kworker 调用 `kernel_write/read`，进度由 **Linux 的 I/O 结果**决定 | Linux 进程调用 `read(2)`，在它自己的上下文里执行 **EVL 的代码**（`do_xbuf_read`） |
| 规则 2 | **违反**：Linux 函数的返回值写进可信状态 | 符合：只依赖用户给的 `count` 和拷贝是否成功 |

也就是说：**如果 Linux 一侧的进度是由运行在 Linux 进程上下文里的可信代码推进的，可信状态就不依赖 Linux 的计算结果**，只依赖用户输入。proxy 的难点在于它要求 Linux 去执行一次真正的文件 I/O，进度只有 Linux 知道。这为回头重新考虑 proxy 提供了一个参照。

## 6. 值得注意的地方

1. **按类型划分在这里表现良好。** 两个 ring 都是 `xbuf_ring`，第一版按字段分析时分不清两组函数指针，只能手工补种子。按分配单元划分，两个 ring 都嵌在 `evl_xbuf` 里，直接跟随它，不需要区分。
2. 两个方向用不同的锁（独立 hard lock 和等待队列的锁），原因见 §2.2 最后一行：outbound 需要在发布数据的同一个临界区里唤醒 oob 读者。
3. `do_xbuf_write` 拷贝失败时用 0 填充没拷到的部分，读者会读到一段 0。注释承认了这一点。
4. `evl_get_xbuf` 的唯一调用者是 latmus：`latmus_oob_ioctl(EVL_LATIOC_RUN)` → `run_measurement` → `measure_continously`（`drivers/evl/latmus.c:790`），所以它是在 oob 上执行的。
5. `rings_show` 无锁读 `fillsz`，只用于显示。
