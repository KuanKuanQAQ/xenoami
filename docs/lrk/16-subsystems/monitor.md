# monitor.c：用户态同步对象的内核后端

> 源码：`kernel/evl/monitor.c`（1475 行）、`include/evl/monitor.h`、`include/uapi/evl/monitor-abi.h`。分析结果：`tools/trust-closure/out/subsys/monitor.txt`。

## 1. 功能

monitor 是 libevl 里大部分同步原语在内核里的实现。一个 monitor 是两种类型之一：

| 类型 | 协议 | 对应 libevl 的 | 语义 |
|---|---|---|---|
| **GATE**（门） | `EVL_GATE_PI` | `evl_mutex`（优先级继承） | 互斥锁 |
| | `EVL_GATE_PP` | `evl_mutex`（优先级天花板） | 互斥锁，持有者临时提升到天花板优先级 |
| **EVENT**（事件） | `EVL_EVENT_GATED` | `evl_event` | 条件变量：必须在持有某个门的情况下等待，等待时释放门 |
| | `EVL_EVENT_COUNT` | `evl_sem` | 计数信号量 |
| | `EVL_EVENT_MASK` | `evl_flags` | 事件标志组：按位等待、按位发送 |

COUNT 和 MASK 两种事件，还可以被普通 Linux 线程用 `read`/`write`/`poll` 在 inband 上操作（比如等一个信号量、发一个标志）。

## 2. 原理

### 2.1 快速路径在用户态：共享状态 `sstate`

每个 monitor 有一块 `__evl_monitor_sstate`，从 **EVL 共享堆**（`evl_shared_heap`）里分配。共享堆通过 `/dev/evl/control` 的 `mmap`（`control.c:404`，`vm_insert_page`）映射进用户进程，所以用户态能直接读写 sstate。

libevl 在没有竞争时**完全不进内核**：

- 加锁：用户态对 `sstate->u.gate.owner`（锁的所有者，一个 fundle 句柄）做 `cmpxchg`，从"无主"改成自己。内核的 `evl_mutex.fastlock` 指向的就是这个字。
- 信号量 P 操作：用户态对 `sstate->u.event.value` 做"大于 0 才减 1"的原子操作。
- 条件变量发信号：用户态在持有门的情况下，直接设置事件 sstate 的 `signaled`、`broadcast` 等标志，再设置门 sstate 的 `signaled`。

只有需要睡眠或者需要唤醒别人时，才进内核（oob ioctl）。

`pollrefs` 告诉用户态"有人在 poll 或者在 inband 上等"。这时用户态即使能走快速路径，也必须进内核发信号，否则 poller 收不到通知。

### 2.2 门（互斥锁）

- `enter_monitor`：先检查当前线程是否已经持有（不支持递归，返回 `-EDEADLK`），提交延迟的天花板（§2.5），然后调用 EVL 核心的 `__evl_lock_mutex_timeout`，成功后把 `nesting` 置 1。
- `exit_monitor` → `__exit_monitor`：如果门的 sstate 有 `signaled` 标志，说明有人在持门期间给它保护的事件发了信号。这时先遍历 `gate->events`，唤醒相应的等待者，再释放 mutex。整个"唤醒 + 解锁"在 `gate->lock` 下完成，对外表现为原子操作。这正是条件变量要求的语义。

### 2.3 条件变量（GATED 事件）

**等待**（`wait_gated_event`）：

1. 按 `req->gatefun` 找到门，检查当前线程确实持有它；
2. 持 `gate->lock`：如果事件还没被任何门跟踪，就挂到这个门的 `events` 链表上（`event->gate = gate`）；如果已经被**别的**门跟踪，返回 `-EBADFD`。注释特别说明：**不信任用户态维护的 `gate_offset`，跟踪关系保存在内核一侧的 `event->gate`**；
3. 把自己挂到事件的等待队列，清掉自己的 `EVL_T_SIGNAL`，然后调用 `__exit_monitor` 释放门（顺带投递其他已发的信号）；
4. 睡眠。醒来后重新进入门；出错的话只解除跟踪，不重新进门，由用户态之后用 `UNWAIT` 重新拿门。

**发信号**：在用户态设标志（§2.1）。真正的唤醒发生在持门者退出门的时候（§2.2），按优先级顺序：广播（全部唤醒）> 定向（只唤醒带 `EVL_T_SIGNAL` 的线程，由 `evl_signal_monitor_targeted` 标记）> 普通（唤醒队首）。

### 2.4 信号量（COUNT）和标志组（MASK）

- **COUNT**：`value` 可以是负数，负数的绝对值就是等待者的数量。
  - `wait_count_oob`：先在锁外试一次；不行就持等待队列锁做 `atomic_dec_return`，结果小于 0 就挂起。出错或超时时把计数加回去。
  - `post_count`：`atomic_inc_return` 的结果仍然 ≤ 0，说明有人在等，唤醒队首。广播则唤醒全部，并把被唤醒的人数加回计数。
- **MASK**：`value` 是位图。
  - 等待者给出 `match_value`，并选择 `exact_match`：要求全部位都到（AND），还是任意一位到（OR）。
  - `post_mask` 先把位或进去，再遍历等待者，满足条件的就唤醒，并**消费**掉它要的位。不是广播的话，唤醒第一个满足条件的等待者就停。

### 2.5 优先级天花板的延迟提交

PP 门在用户态快速加锁时，内核并不知道，没法立即提升优先级。于是 libevl 把这个门的句柄写进**线程的** sstate：`curr->sstate->pp_pending`。

调度器在当前线程可能被切走之前（`sched/core.c:953`），调用 `evl_commit_monitor_ceiling()`：按句柄找到门，提交天花板优先级，然后清空 `pp_pending`。如果线程在被切走之前就已经释放了这个门，`__exit_monitor` 会直接清掉 `pp_pending`。

### 2.6 inband 等待者

Linux 线程在 COUNT 或 MASK 上 `read()`，走 `wait_monitor_inband`。它是一个宏展开出来的等待循环：

- 同时持 Linux 等待队列的锁和 EVL 等待队列的锁，避免和 `post_*` 之间丢失唤醒；
- 用 `schedule()` / `schedule_timeout()` 睡眠。

`post_*` 如果在 oob 上被调用，就不能直接 `wake_up`，于是先 `waitqueue_active` 看看有没有 inband 等待者，有的话通过 irq_work（`inband_wake_r`）推迟到 inband 再唤醒。

## 3. 数据结构

### 3.1 `struct evl_monitor`

| 字段 | 适用类型 | 含义 | 谁写 |
|---|---|---|---|
| `element` | 两者 | EVL element | factory |
| `sstate` | 两者 | 指向共享堆里的共享状态 | build，之后不变 |
| `type`、`protocol` | 两者 | 类型和协议。**内核只信这两个字段，不信 sstate 里的副本**（`monitor.c:1348-1364` 的注释） | build，之后不变 |
| `mutex` | GATE | EVL 核心的互斥锁，`fastlock` 指向 `sstate->u.gate.owner`，PP 的 `ceiling_addr` 指向 `sstate->u.gate.ceiling` | build；之后由加锁、解锁操作修改 |
| `events` | GATE | 这个门正在跟踪的事件链表 | `wait_gated_event` 加入，`untrack_event` 移除 |
| `lock` | GATE | hard lock，保护 `events` 和"唤醒 + 解锁"序列 | — |
| `wait_queue` | EVENT | oob 等待者队列；它的 `wchan.lock` 也保护 COUNT/MASK 的计数操作 | — |
| `inband_wait_r` / `inband_wait_w` | EVENT | Linux 等待队列：inband 读者（等事件）/ inband poller 等 POLLOUT | — |
| `inband_wake_r` / `inband_wake_w` | EVENT | 从 oob 唤醒 inband 等待者用的 irq_work。**`inband_wake_w` 初始化了但从来没被挂过**（§6） | build |
| `gate` | EVENT（GATED） | 当前跟踪这个事件的门，只在有人等待时有效 | `wait_gated_event`、`untrack_event` |
| `poll_head` | EVENT | oob poll 的等待点 | — |
| `next` | EVENT | 挂在 `gate->events` 上的链表节点 | 同 `gate` |

### 3.2 `struct __evl_monitor_sstate`（共享堆，用户态可直接写）

| 字段 | 含义 | 内核怎么用 |
|---|---|---|
| `shdr.fundle` | 这个 monitor 的句柄 | build 时写，给用户态参考 |
| `type`、`protocol` | 类型和协议的副本 | **只是给用户态看的，内核从不读** |
| `flags.signaled` / `broadcast` / `targeted` | 条件变量的待投递信号。门的 sstate 也用 `signaled` 表示"有事件被发了信号" | `__exit_monitor`、`wakeup_waiters` 读并清零；`evl_signal_monitor_targeted` 设置 |
| `u.gate.owner` | 互斥锁的所有者句柄（当作 `atomic_t` 用） | EVL mutex 核心通过 `fastlock` 读写 |
| `u.gate.ceiling` | PP 天花板优先级 | mutex 核心通过 `ceiling_addr` 读 |
| `u.gate.recursive`、`nesting` | 递归属性和嵌套计数 | 递归由用户态处理，内核只在进门时把 `nesting` 置 1 |
| `u.event.value` | COUNT 的计数或 MASK 的位图（当作 `atomic_t` 用） | 所有 wait/post/trywait 都读写它 |
| `u.event.pollrefs` | 正在 poll 或者在 inband 上等的人数 | poll 时加 1，取消监视时减 1，inband 等待期间加 1 |
| `u.event.gate_offset` | 正在跟踪这个事件的门的 sstate 在共享堆里的偏移，没有则为 `EVL_MONITOR_NOGATE` | 内核写，给用户态参考；内核自己用 `event->gate` |

### 3.3 其他

| 结构 | 含义 |
|---|---|
| `evl_mask_wait`（栈上，挂在 `curr->wait_data`） | MASK 等待者想要的位 `value` 和匹配方式 `exact_match`；`post_mask` 唤醒时把实际拿到的位写回 `value` |
| `evl_monitor_attrs`（用户输入） | `clockfd`、`type`、`protocol`、`initval` 或 `ceiling`/`recursive` |
| `evl_monitor_waitreq` 等（用户输入） | 超时指针、门句柄 `gatefun`、匹配值 `value` |

## 4. 函数语义

| 函数 | stage | 语义 |
|---|---|---|
| `get_monitor_by_fundle(fundle, type)` | 两者 | 在 EVL 核心命名空间里按句柄查 monitor 并取引用；类型不对就放掉引用、返回 NULL |
| `evl_signal_monitor_targeted(target, eventfun)` | oob（线程的 ioctl） | 如果 `target` 正在等这个事件，就设置事件 sstate 的 `targeted`、`signaled`，并给线程加上 `EVL_T_SIGNAL`。注释要求调用者持有门 |
| `__evl_commit_monitor_ceiling` | oob（调度器） | §2.5 |
| `untrack_event` | oob，持两把锁 | 事件没有等待者了，就从门的跟踪链表里摘掉 |
| `wakeup_waiters` | oob，持两把锁 | 按广播 > 定向 > 普通唤醒，然后清零事件的标志 |
| `__enter_monitor` / `enter_monitor` / `tryenter_monitor` | oob | 拿门（§2.2） |
| `__exit_monitor` / `exit_monitor` | oob | 先投递信号再释放门（§2.2） |
| `__trywait_count` | 两者 | 计数大于 0 时减 1（cmpxchg 循环），否则 `-EAGAIN` |
| `trywait_count_locked` | 持等待队列锁 | 成功则放锁，给 inband 等待宏用 |
| `wait_count_oob` | oob | §2.4 |
| `post_count(mon, sigval, bcast)` | 两者 | §2.4。`sigval == 0` 表示只触发 poll 通知，不改计数；最后唤醒 inband 等待者（在 inband 上直接 `wake_up`，在 oob 上走 irq_work） |
| `inband_wake_r_irqwork` / `inband_wake_w_irqwork` | inband | irq_work 回调：`wake_up`，然后放掉 element 引用 |
| `trywait_mask_locked` / `trywait_mask` | 两者 | 按匹配规则从位图里取位；取完后位图为空的话发 POLLOUT |
| `wait_mask_oob` | oob | MASK 的等待：取不到就挂起，`curr->wait_data` 指向栈上的 `evl_mask_wait` |
| `post_mask` | 两者 | §2.4 |
| `wait_gated_event` | oob | §2.3 |
| `wait_monitor_oob` | oob | 按协议分派：没有给门句柄的话按 COUNT/MASK 处理，否则按 GATED |
| `unwait_monitor_oob` | oob | 出错返回后重新拿门 |
| `wait_monitor_inband` | inband | §2.6；COUNT 等待期间把 `pollrefs` 加 1，强制用户态进内核发信号 |
| `__monitor_common_ioctl` | 两者 | WAIT（按 stage 分到 oob 或 inband 版本，inband 版本不支持 GATED）、TRYWAIT、SIGNAL/BROADCAST |
| `__monitor_ioctl` / `monitor_ioctl` | inband | 加上 BIND：把类型、协议、minor、sstate 偏移、句柄返回给用户态 |
| `__monitor_oob_ioctl` / `monitor_oob_ioctl` | oob | 加上 ENTER/TRYENTER/EXIT/UNWAIT |
| `evl_functl_monitor` | 两者 | `functl` 系统调用的入口，按 stage 分派到上面两个 |
| `monitor_oob_poll` / `monitor_unwatch` | oob | COUNT/MASK：登记 poll 并把 `pollrefs` 加 1，取消监视时减 1；GATED 返回 POLLERR；GATE 总是可读 |
| `monitor_poll` | inband | 只支持 MASK |
| `monitor_read` / `monitor_write` / `monitor_oob_read` / `monitor_oob_write` | 各自的 stage | 按 4 字节值读写：读等于等待，写等于发送（COUNT 写 0 表示广播） |
| `monitor_release` | inband | 事件：踢醒 oob 等待者；门：`evl_flush_mutex` |
| `monitor_factory_build` | inband | 校验属性（PP 的天花板范围、PI 不能带天花板、协议合法），按 fd 取时钟，分配对象和 sstate，按类型初始化，登记命名空间 |
| `monitor_factory_dispose` | inband | 移出命名空间，放时钟，销毁等待队列或 mutex，从门的跟踪链表摘下，释放 sstate |
| `state_show` | inband（sysfs） | 显示状态 |

## 5. 可信域划分

按 [README 的规则 1–10](README.md#划分规则)。字段级的原始结果在 `tools/trust-closure/out/subsys/monitor.txt`（种子见 `cfg/monitor.json`），下面是在它基础上人工推导的类型级结论。

### 5.1 代码：入口和调用链（规则 1、2）

| 入口 | 调用者 | 进入的函数 | 参数 | 结论 |
|---|---|---|---|---|
| oob 系统调用 | EVL 核心分派 | `monitor_oob_read/write/ioctl/poll` | `filp` 来自 `evl_file`；请求结构体（门句柄、超时、匹配值）是用户输入 | 可信 |
| oob 系统调用 | `syscall.c` 的 functl 分派 | `evl_functl_monitor` | `fundle` 按值传入，在 EVL 的命名空间表里查 | 可信；分派代码也是可信代码 |
| oob 线程 ioctl | `thread.c` 的 `EVL_THRIOC_SIGNAL` | `evl_signal_monitor_targeted` | `eventfun` 按值传入（`raw_get_user`），在命名空间表里查 | 可信；thread.c 的这条路径也是可信代码 |
| 调度器 | `sched/core.c:953`（返回用户态前）、`monitor.c` 自己 | `__evl_commit_monitor_ceiling` | **隐式状态**：读当前线程 sstate 里的 `pp_pending` | 可信；`pp_pending` 在共享堆里，见 5.2 |
| poll 核心 | EVL poll 核心回调 | `monitor_unwatch` | `head` 指针来自 EVL 的 poll 核心，`container_of` | 可信 |
| inband 系统调用 | VFS（Linux） | `monitor_ioctl/read/poll` | `filp` 是 Linux 给的 | 可信代码（改计数、等待队列），入口在 Linux，找对象要核对（README 结论 3） |
| 关闭 | VFS | `monitor_release` | 同上 | 可信（刷新等待队列或 mutex） |
| inband 唤醒 | Linux 的 irq_work | `inband_wake_r_irqwork`、`inband_wake_w_irqwork` | `work` 指针来自 Linux | **问题**：先 `wake_up`（Linux 对象），再 `evl_put_element`（**写可信的引用计数**）。这是"可信代码从 Linux 入口拿到不可信指针后写可信状态"，规则 1 不允许。换成 EVL 自己的中继后消失（5.4） |
| element 释放 | Linux 的 irq_work（`do_put_element_irq`） | `monitor_factory_dispose` | 同上 | element 生命周期的共性问题（README 结论 1） |

规则 2 的检查：inband 等待循环（`__wait_monitor_inband` 宏）调用 `signal_pending()`、`schedule_timeout()`，醒来后在可信锁下重新 trywait，**符合**。`irq_work_queue` 在 oob 上调用、而且返回值决定 `evl_put_element`，**不符合**。

### 5.2 分配单元（规则 4、5、6、10）

| 分配单元 | 分配点 | 结论 |
|---|---|---|
| `struct evl_monitor` | `monitor_factory_build`：`kzalloc(sizeof(*mon))` | **可信** |
| `struct __evl_monitor_sstate` | `evl_zalloc_chunk(&evl_shared_heap, ...)`，映射给用户态 | **不能可信**（规则 10）：用户态直接写，所有值都是用户输入 |
| 线程的 sstate（`pp_pending` 所在） | 同样在共享堆里 | 同上 |
| `evl_mask_wait` | **等待者的栈**，地址存进 `curr->wait_data` | 地址被发布了：`post_mask` 在**另一个线程**里通过它写回拿到的位。所以不是规则 3 的"自己的副本"，按规则 4 处理：写者是 `wait_mask_oob`（自己）和 `post_mask`，都可信。它依赖线程栈可信（运行时底座） |
| `evl_monitor_attrs`、各种 waitreq | 栈，`copy_from_user` | 用户输入，按值，规则 3 |

`evl_monitor` 里嵌入的成员按规则 6 跟随它：`element`；GATE 的 `mutex`（`evl_mutex`）、`events`、`lock`；EVENT 的 `wait_queue`、`gate` 指针、`poll_head`、`next`。**例外是 5.4 的 Linux 成员。**

**可信对象指向不可信内存。** `mon->sstate`、`mutex.fastlock`（指向 `u.gate.owner`）、`mutex.ceiling_addr`（指向 `u.gate.ceiling`）是可信字段，值是指向共享堆的地址。规则 10 允许可信代码持有这样的地址、往里写，但读到的值必须校验或者只当提示。EVL 现在的情况：

| 从共享堆读的值 | 现在怎么处理 | 是否满足规则 10 |
|---|---|---|
| `sstate->type`、`protocol` | 内核从不读，只信 `mon->type/protocol`（`monitor.c:1348-1364`） | 满足 |
| `u.event.gate_offset` | 内核从不读，只信 `event->gate`（`:640-645`） | 满足 |
| 线程 sstate 的 `pp_pending` | 当句柄在命名空间表里查，查不到就跳过（`:77-79`） | 满足：校验 |
| `flags.signaled/broadcast/targeted` | 决定唤醒谁、唤醒几个 | **只当提示**：错了只会多唤醒或少唤醒，被唤醒者会重新检查。是否满足要看"少唤醒"能不能导致实时任务永久阻塞 |
| `u.event.value`（计数、位图） | 决定 wait 是否成功、post 唤醒谁 | 这是快速路径的设计，用户态本来就要改它。用户态改错只影响使用这个 monitor 的进程自己——**前提是别的进程改不了它**（5.6） |
| `u.event.pollrefs` | 决定要不要发 poll 事件 | 只当提示 |
| `u.gate.owner`（经 `fastlock`） | mutex 核心据此判断所有者、做优先级继承 | **要看 `mutex.c`**：任意值会不会导致越界、错误的优先级提升 |

### 5.3 `evl_monitor` 类型的写者（规则 4）

| 写者 | 写什么 |
|---|---|
| monitor.c 的 oob 路径（`enter/exit_monitor`、`wait_*_oob`、`post_*`、`untrack_event` 等） | `events`、`gate`、`next`，经 EVL 核心写 `mutex`、`wait_queue`、`poll_head` |
| `monitor_factory_build/dispose`、`monitor_release` | 初始化、销毁、刷新 |
| inband 路径（`wait_monitor_inband`、`monitor_ioctl`、`monitor_read`） | 计数（在共享堆里）、等待队列 |
| EVL 核心：`mutex.c`、`wait.c`、`poll.c`、`factory.c` | 嵌入的 EVL 成员，对所有实例都一样 |
| **Linux：等待队列代码、irq_work 核心、RCU、cdev** | 嵌入的 Linux 成员，见 5.4。按规则必须消除 |

### 5.4 嵌入的 Linux 成员（规则 8）

| 成员 | 谁写 | 现在怎么用 | 改法 |
|---|---|---|---|
| `inband_wait_r`（`wait_queue_head`） | Linux 等待代码 | inband 等待者在上面睡；**oob 的 `post_count`/`post_mask` 用 `waitqueue_active` 偷看它** | 单独分配在不可信域；偷看改成 EVL 自己的"inband 等待者数"，由 `wait_monitor_inband` 增减 |
| `inband_wait_w`（`wait_queue_head`） | 同上 | inband poller 等 POLLOUT；只在 inband 上 `wake_up` | 单独分配在不可信域 |
| `inband_wake_r`（`irq_work`） | Linux irq_work 核心 | oob 上 `irq_work_queue`，**返回值决定要不要 `evl_put_element`** | 换成 EVL 自己的 inband 中继，引用计数由 EVL 自己管 |
| `inband_wake_w`（`irq_work`） | — | **死代码**：初始化了，从来没挂过 | 直接删除 |
| `element` 里的 `rcu`、`cdev`、`irq_work` 等 | RCU、字符设备核心、irq_work | 所有 element 共有 | EVL 核心拆分 `evl_element`（README 结论 1） |

### 5.5 依赖的 Linux 分配单元（规则 9）

| Linux 对象 | 字段 | 在哪里读 | 截断方式 |
|---|---|---|---|
| `struct file` | `f_flags`（`O_NONBLOCK`） | 所有 wait 路径，含 oob | 镜像（M3） |
| `struct file` | `private_data` | `element_of`，含 oob | 从 `evl_file` 找对象（M4） |
| `mm_struct` 里的 oob 状态 | `dovetail_mm_state()` | 按句柄查 monitor 时（`evl_lookup_ns`），私有 element 只对本进程可见：这是一个**访问控制判断** | 运行时底座，写成信任假设（M5）。属于 EVL 核心的命名空间代码 |

### 5.6 小结

| 问题 | 结论 |
|---|---|
| 可信代码 | monitor.c 里除两个 irq_work 回调外的全部函数（回调改掉后不复存在）；调用链上的 `syscall.c` functl 分派、`thread.c` 的 `EVL_THRIOC_SIGNAL`、`sched/core.c` 的 ceiling 提交 |
| 可信分配单元 | `evl_monitor` |
| 不可信但被可信代码读 | `__evl_monitor_sstate`、线程 sstate（用户态可写，规则 10） |
| 需要改的地方 | 两个等待队列头移出、`inband_wake_r` 换掉、`inband_wake_w` 删除（规则 8）；`f_flags`、`private_data` 截断（规则 9）；inband 入口和释放入口核对指针（规则 1）；确认 mutex 核心对任意 `fastlock` 值健壮（规则 10） |

**规则 10 的关键前提：别的进程改不了这个 monitor 的 sstate。** 用户态改自己 monitor 的计数，后果只落在自己身上，这可以接受。但 `control_mmap` 把**整个**共享堆插入调用者的地址空间，所有进程的所有 monitor 和线程的 sstate 都在同一个堆里。所以任何能打开 `/dev/evl/control` 并调用 mmap 的进程，**包括非实时进程**，都能改写任何 monitor 的 sstate，导致实时任务错过唤醒或者被永久阻塞。这需要实际验证：设备节点的权限可能已经把访问限制在实时组里。如果没有，就需要按进程（或按共享组）划分共享堆。

## 6. 值得注意的地方

1. **monitor 是规则 10 的主要案例。** 快速路径要求用户态能直接改锁和计数，这和"可信数据只能由可信代码写"天然冲突。EVL 的作者已经做了一部分防护（5.2 表里"满足"的几行），剩下的问题集中在两处：共享堆的映射范围，和 mutex 核心的健壮性。
2. **`evl_mask_wait` 说明了规则 3 的例外**：栈对象一旦把地址发布出去，就会被别的线程写，不再是"只有自己能写的副本"。
3. **`evl_signal_monitor_targeted` 的竞争**：注释承认，调用者没持门的话可能丢事件（"Too bad"）。
4. **MASK 的 `value` 仍然是原子类型，只是为了 ABI 兼容**（`:553-561` 的注释）。它所有的修改其实都在等待队列锁下。
5. 条件变量的信号投递推迟到"退出门"的时候，所以用户态发了信号但不退出门的话，等待者不会醒。这是有意的设计：唤醒和释放门必须作为一个整体完成，被唤醒的线程随后还要重新拿门。
