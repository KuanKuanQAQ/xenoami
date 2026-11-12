# 24 EVL 核心的截断：Z → z 通知全部只记录，以及几处 Z 读 Linux 状态的截断

> 2026-11-11。按 doc 22 的方法，对 EVL 核心（不含 xbuf、proxy，它们见 doc 22）逐个文件找 Z/z 交集并截断。**已实现并测试**，改动未提交。
> 可以单独阅读；术语和方法的完整说明见 [22 §1–§5](22-pipe-final-design.md)。doc 23 预留给信任基清单（路线图 P1）。

---

## 0. 结论

1. **EVL 核心里，Z 要通知 z 的地方一共 9 类，全部改成"只记录、不调用"。** 以前它们都在 oob 上调用 Linux 的 `irq_work_queue()`，可信对象里也嵌着 Linux 的 `struct irq_work`。现在可信侧只把自己内存里的一条记录挂到可信的 per-CPU 链表上，再发一个合成中断，由 inband 侧自己执行 `wake_up`、`complete`、`wake_up_process`、`queue_work`、`send_sig`。EVL 核心编译进来的代码里不再有 `irq_work`。
2. **Z 读 Linux 状态的地方，截断了 4 处**：
   - oob 系统调用的权限检查不再读 Linux 的凭据；
   - `clock_sleep` 的重启日期保存在可信线程里；
   - oob 信号不再每次从可信堆分配，改为每个线程一条固定的记录；
   - monitor 的 oob 写不再调用会 `might_fault()` 的 `copy_from_user`。
3. **monitor、observable 的 Linux 等待队列移出了可信对象**，单独分配，和 doc 22 里 xbuf、proxy 的做法一样。
4. **wait、timer、mutex、sem、sched 的 fifo/idle/weak 本来就不碰 Linux**，不需要改。
5. **最大的剩余交集是 oob 系统调用经 Linux 的 fd 表找对象**（doc 20 的 L1），要改 libevl 的 ABI，这次没有做，见 §8。
6. **测试**：initramfs 补上了缺的三个共享库，这样原来因为环境问题跑不起来的 6 个测试也能跑了。新基线是 78 个测试过 74 个：
   - 改动前的 HEAD：74/78；
   - 改动后：74/78，失败的 4 个相同；
   - 没有 WARNING。

---

## 1. 方法回顾

- **Z**：oob 代码，加上它依赖的数据，再加上这些数据的写者，如此闭包。
- **z**：非实时线程能到达的代码，加上它写的数据，再加上这些数据的读者，如此闭包。
- 两者的交集要截断。交集分两种颜色：
  - **黄色**：Z 受 z 影响。z 写的值、z 持有的锁会影响 Z 的内存、时序或控制流。
  - **紫色**：Z 要通知 z。按 doc 22 的结论，紫色也要处理，处理方法统一是"只记录不调用"，不保证投递。
- **截断方法**：私有副本、输入化、只记录不调用、归属单侧、消除。
- **明确放弃的**：z 侧内容的正确性、z 的活性（z 不干活）、唤醒是否精确。

这次额外用到一条判断：**只影响调用者自身的 z 输入**。例如 `current->ptrace`、`signal_pending(current)`，它们只决定当前线程自己的走向，比如切到 inband、返回 `-EINTR`。z 改这些值，最多让这个线程自己失去实时性，影响不到其他实时线程和可信状态。这和 doc 22 里"阻塞的调用者自担风险"是同一类。这类输入记录在表里，但不截断。

---

## 2. 盘点：EVL 核心和 Linux 的接触面

工具：`grep` 出 `irq_work`、`wake_up`、`complete`、`queue_work`、`current->`、`fget`、凭据访问等，再逐处读代码。doc 17 的自动分析（§5.2、§5.3）作为清单来源。

### 2.1 紫色：Z 要通知 z

| # | 流程 | 文件 | 原来 | 记录的类型 |
|---|---|---|---|---|
| P1 | 线程切到 inband：oob 调度器让出后，唤醒对应的 Linux 任务 | `sched/core.c` `evl_switch_inband_details`、`thread.c` | `irq_work_queue(&curr->inband_work)` → `wake_up_process` | `WAKE_TASK`，cookie 是 `task_struct` |
| P2 | 内核线程启动：在 oob 上进入 dormant 后，通知 inband 的创建者 | `thread.c` `map_kthread_self` | `irq_work` → `complete(&kthread->done)` | `COMPLETE` |
| P3 | 给线程发信号（HM 的 SIGDEBUG、cancel 的 SIGTERM） | `thread.c` `evl_signal_thread` | **每次 `evl_alloc` 一个 `sig_irqwork_data`** + `irq_work` → `send_sig_info` | `CALL`，每线程一条记录，信号位累加 |
| P4 | monitor（event）：oob 上 post 之后唤醒 inband 等待者 | `monitor.c` | `waitqueue_active` + `irq_work`（取 element 引用）→ `wake_up` | `WAKE_UP` |
| P5 | observable：唤醒读者/写者；oob 上丢掉的观察者要在 inband 释放 | `observable.c` | 3 个 `irq_work`（取 element 引用） | `WAKE_UP` ×2，`CALL`（释放） |
| P6 | stax：oob 解锁后唤醒 inband 等待者 | `stax.c` | `irq_work` → `wake_up_all` | `WAKE_ALL` |
| P7 | `evl_call_inband`：oob 请求在 inband 执行一个函数 | `work.c` | `irq_work` → `queue_work` | `CALL` |
| P8 | crossing：最后一个 oob 用户离开，放行 inband 的等待者（文件释放） | `crossing.h`、`file.c` | `irq_work` → `complete` | `COMPLETE` |
| P9 | element 的最后一次 put 发生在 oob 上，要到 inband 销毁 | `factory.c` | `irq_work` → 挂 flusher 队列 | `CALL`，一次性 |

xbuf、proxy 的通知在 doc 22 里已经改过，这次只是跟着新接口调整。

### 2.2 黄色：Z 受 z 影响

| # | 交集 | 位置 | 影响 | 处理 |
|---|---|---|---|---|
| Y1 | 可信对象里嵌着 `struct irq_work`：Linux 的 `irq_work_run` 会写它的 flags 和链表节点；`irq_work_queue` 是 Linux 代码 | P1–P9 | 可信内存被 Linux 写；可信代码调用 Linux 代码 | **已截断**：只记录不调用（§3） |
| Y2 | 可信对象里嵌着 Linux 等待队列头：Linux 会把等待项链进来 | monitor、observable | 可信内存被 Linux 写 | **已截断**：归属单侧，等待队列单独分配 |
| Y2' | 同上 | stax `inband_wait`、crossing `passed`、evl_work `wq_work`、element 的 `cdev`/`flush`/`rcu` | 同上 | **未截断**，见 §8 |
| Y3 | oob 信号每次从可信堆分配，由 inband 释放 | `evl_signal_thread` | z 不投递，可信堆就会被耗尽，所有可信组件的分配都会失败 | **已截断**：每个线程一条固定记录，信号合并 |
| Y4 | oob 系统调用每次都读 Linux 凭据：`cap_raised(current_cap(), CAP_SYS_NICE)` | `syscall.c` `do_oob_syscall` | Linux 改了凭据，就能让任何 oob 系统调用返回 `-EPERM` | **已截断**：私有副本（§4.9） |
| Y5 | `clock_sleep` 被信号打断后，重启日期存放在 Linux 的 `current->restart_block.nanosleep.expires` 里 | `clock.c` | Linux 改了它，就决定了实时线程睡到什么时候 | **已截断**：私有副本，存在 `evl_thread.restart_expires` |
| Y6 | monitor 的 oob 写调用 `copy_from_user`，其中的 `might_fault()` 是 inband 代码 | `monitor_common_write` | doc 17 §5.1 的冲突 | **已截断**：`access_ok` + `raw_copy_from_user` |
| Y7 | oob 系统调用经 Linux 的 fd 表、`f_op`、`private_data` 找对象 | `syscall.c`、`file.c`、`poll.c` | Linux 可以把 fd 换成另一个对象 | **未截断**：句柄表，doc 20 |
| Y8 | `evl_current()` 经 `thread_info` 里的 Dovetail 字段；per-CPU 寻址 | 全局 | 运行时底座 | 归入信任基（doc 23） |
| Y9 | tick 在 oob 上调用 Linux 时钟设备驱动的 `set_next_event` | `tick.c` | 硬件抽象层 | 归入信任基（doc 23） |
| Y10 | `current->ptrace`（trap 处理）、`signal_pending(current)`（`evl_switch_oob`、`clock_sleep`） | `thread.c`、`clock.c` | 只影响当前线程自身 | 记录，不截断（§1） |
| Y11 | 用户共享内存：monitor、mutex 的 sstate 和 fastlock | `monitor.c`、`mutex.c` | 规则 10：当作输入 | EVL 原本就把它当输入；逐处审计记为 N3 |
| Y12 | oob 路径普遍用 `raw_copy_*_user` 但不做 `access_ok` | 多处 | 用户可以传内核地址 | 记为 N4；这次只修了 Y6 一处 |

### 2.3 不碰 Linux 的文件

`wait.c`、`timer.c`、`mutex.c`、`sem.c`、`sched/fifo.c`、`sched/idle.c`、`sched/weak.c`：没有发现调用 Linux 函数、读写 Linux 对象的地方，只用内联的链表、锁、原子操作。它们构成 EVL 核心里本来就纯可信的部分，不需要改。

---

## 3. 通知中继第二版（`inband_notify.c/h`）

### 3.1 流程和标记

一次通知要用到以下标记：

| 标记 | 放在哪里 | 谁写 | 谁读 |
|---|---|---|---|
| `PENDING` 位 | 记录（可信） | Z 在 post 时置位；取记录的门清除 | Z、门 |
| `BUSY` 位 | 记录 | 门置位；投递完成后清除 | drain（可信，inband 任务上下文） |
| `ONESHOT` 位 | 记录 | 初始化时设置 | 门 |
| per-CPU 链表 | 可信 | Z 用 `llist_add` 加入；门用 `llist_del_first` 取出，关硬中断 | 门 |
| `type`、`tag`、`cookie`、`fn` | 记录 | 初始化时设置 | 门复制一份交给 z |
| `bits`（累加） | 记录 | Z 原子 OR | 门原子 `xchg` 取走 |
| `value`（最后一次的值） | 记录 | Z | 门复制 |
| 合成中断 | Dovetail（信任基） | Z 调用 `irq_post_inband` | inband 调度 |

z 只能拿到这条记录的**副本**（`struct evl_notify_event`），不能写记录本身。cookie 指向 z 的对象，比如等待队列头、`task_struct`、`completion`，可信侧从不解引用它。

### 3.2 记录的类型

| 类型 | z 做什么 | 用在哪里 |
|---|---|---|
| `EVL_NOTIFY_WAKE_UP` | `wake_up(cookie)` | xbuf、monitor、observable |
| `EVL_NOTIFY_WAKE_ALL` | `wake_up_all(cookie)` | stax |
| `EVL_NOTIFY_WAKE_TASK` | `wake_up_process(cookie)` | 线程切到 inband |
| `EVL_NOTIFY_COMPLETE` | `complete(cookie)` | kthread 启动、crossing |
| `EVL_NOTIFY_CALL` | `fn(&event)` | proxy 叫醒转发者、`evl_work`、信号、observable 的释放、element 销毁 |

`CALL` 分两种。一种是 **z 的动作**，比如 `queue_work`、`send_sig`，`fn` 只用副本里的 cookie。另一种是**可信侧推迟到 inband 执行的工作**，比如 observable 释放观察者、element 进 flusher 队列。后一种相当于由 z 调用的门：z 可以推迟它、不调用它，代价只是内存晚释放（活性），所以这类函数必须在对象存活期间的任何时刻调用都安全。

### 3.3 和第一版相比

| 改动 | 原因 |
|---|---|
| 新增 `WAKE_ALL`、`WAKE_TASK`、`COMPLETE`、`CALL`，删去专用的 `PROXY` | 9 类通知都要用 |
| 新增 `bits` 和 `value`，取记录时先清 `PENDING` 再 `xchg` 取 `bits` | 信号要合并（Y3）。顺序保证：与投递并发的 post，要么看到 `PENDING` 还没清、它的位被这次取走；要么重新排队 |
| 新增 `ONESHOT`：门不置 `BUSY`，调用 `fn` 之后不再碰记录 | element 的最后一次 put：`fn` 把 element 交给 flusher 以后，element 随时可能被释放 |
| 在 inband 上 post，如果中断开着就立即同步中断日志（`sync_current_irq_stage()`，和 `irq_local_work_raise()` 的做法一样） | 可信代码在 inband 上运行时（例如 inband 的 ioctl）也只 post，不直接调用 `wake_up`。不同步的话，投递要等到下一次中断或解除 stall 才发生 |

### 3.4 生命周期规则

- **原来**：每次 `irq_work_queue` 之前先 `evl_get_element`，处理函数里再 put。这有两个问题：
  - 如果处理函数里的 put 是最后一次，就会在中断上下文里直接调用 `__do_put_element`，因为这时硬中断是开着的，`current_work()` 也为假；
  - 每次通知都要做一次引用计数。
- **现在**：post 的调用者本来就持有对象的引用（oob 系统调用持有文件引用）。对象的 dispose 在引用归零以后、inband 任务上下文里执行，先对每条记录 `evl_inband_notify_drain()`，再释放对象和 inband 侧的结构。引用归零以后不会再有 post，所以 drain 结束后，中继不会再碰这条记录。
- **例外**：
  - proxy 叫醒转发者时仍然带引用（doc 22），因为转发者的 work 要在对象上运行。
  - `evl_work` 保留原来的"受保护 element"引用语义。
  - element 自己的销毁记录是一次性的（`ONESHOT`），不 drain。

---

## 4. 每个文件的改动

### 4.1 `include/evl/inband_notify.h`、`kernel/evl/inband_notify.c`

第二版中继，见 §3。接口：

- `evl_init_inband_notify(n, type, tag, cookie)`：初始化记录；
- `evl_init_inband_call(n, fn, cookie, tag)`：初始化 `CALL` 类型的记录；
- `evl_set_inband_notify_oneshot(n)`：设为一次性记录；
- `evl_inband_notify_post(n)`：post；
- `evl_inband_notify_post_bits(n, bits)`：累加 `bits` 后 post；
- `evl_inband_notify_set_value(n, v)`：设置下一次投递带的值；
- `evl_inband_notify_drain(n)`：等记录空闲；
- `evl_inband_notify_idle(n)`：记录是否空闲；
- `EVL_INBAND_NOTIFY_INIT(...)`：静态初始化。

### 4.2 `monitor.c`、`monitor.h`（P4、Y2、Y6）

- `inband_wait_r/w` 移进单独分配的 `struct evl_monitor_ib`，只有 event 类型的 monitor 才分配。可信对象里只留指针和两条记录 `notify_r`、`notify_w`。
- `notify_inband_readers()`、`notify_inband_writers()`：`smp_mb()` 之后，用 `waitqueue_active()` 读 inband 内存当作输入提示，为真才 post。提示错了，最多多 post 一次或漏掉一次 inband 唤醒。这和 xbuf 用 `ib_waiting` 提示是同一种做法。
- oob 和 inband 两条路径的唤醒都改成 post。原来 inband 路径直接 `wake_up`，现在改为 post 后立即同步投递。
- 删掉两个 irq_work 处理函数，以及唤醒时对 element 的 get/put。
- dispose：先 drain `notify_r`、`notify_w`，再 `kfree(mon->ib)`。创建失败的路径补上 `kfree(ib)`。
- `monitor_common_write` 也会被 `oob_write` 调用，所以改成 `access_ok` + `raw_copy_from_user`，去掉 `might_fault()`，解决 doc 17 §5.1 的那个冲突。

### 4.3 `observable.c`、`observable.h`（P5、Y2）

- `inband_wait_r/w` 移进 `struct evl_observable_ib`，`evl_alloc_observable` 时单独分配；线程的 observable 也走这条路径。
- 3 个 irq_work 换成 `notify_r`、`notify_w`（`WAKE_UP`）和 `flush_call`（`CALL`，调用 `do_flush_work`，这是一个推迟到 inband 执行的门）。
- 订阅时、`wake_up_observers`、写端恢复可写时，统一改用 `notify_inband_readers/writers()`。
- dispose：先 drain 三条记录，再 `kfree(observable->ib)`。

### 4.4 `stax.c`、`stax.h`（P6）

- irq_work 换成一条 `WAKE_ALL` 记录。`unlock_from_oob` 在并发计数归零时 post。
- `evl_destroy_stax` 先 drain，再销毁 oob 等待队列。`EVL_STAX_INBAND_SPIN` 模式没有 inband 等待者，跳过 drain。
- `inband_wait` 还嵌在 stax 里（Y2'）：stax 嵌在驱动的结构里，`evl_init_stax` 不返回错误码，要移出就得改接口。

### 4.5 `work.c`、`work.h`（P7）

- `struct evl_work` 里的 `irq_work` 换成 `kick`，一条 `CALL` 记录，`fn = __evl_do_inband_kick`，在 inband 上调用 `queue_work`。
- 静态初始化宏 `EVL_DEFINE_WORK`、`EVL_DEFINE_SYNC_WORK` 改用 `EVL_INBAND_NOTIFY_INIT`。
- `evl_flush_work`、`evl_cancel_work` 原来用 `irq_work_sync` 等待，现在改用 drain。
- 保留 element 引用的语义：post 失败（已经挂起）就 put；`queue_work` 失败（已经排队）也 put。

### 4.6 `crossing.h`（P8）

- irq_work 换成一条 `COMPLETE` 记录，cookie 是 `&c->passed`。
- `evl_up_crossing` 一律 post。从 inband 调用、中断开着的时候，会立即同步投递，效果和原来直接调用 `complete` 一样。
- `evl_pass_crossing` 在 `wait_for_completion` 之后再 drain 一次：`complete()` 以后中继还要清 `BUSY`，调用者随后会释放 `evl_file`，不 drain 就会在释放后访问。原来的 irq_work 也有同样的窗口，Linux 会在处理函数返回后写 `work->flags`。

### 4.7 `factory.c`、`factory.h`（P9，以及 doc 20 的 B1–B3）

- `evl_element.irq_work` 换成 `put_call`。oob 上（或硬中断关着时）的最后一次 put：初始化一条一次性 `CALL` 记录并 post，`fn = put_element_call` 把 element 挂到 flusher 队列。workqueue 里的 put 直接调用 `queue_flush_element`。
- **B1**：`flusher_kthread` 原来是一个全零的静态 `task_struct`，`kthread_stop(&flusher_kthread)` 停的不是真正的线程。改成保存 `kthread_run` 返回的指针。
- **B2**：`getname_kernel` 出错时返回 `ERR_PTR`，原来检查的是 `== NULL`。改为 `IS_ERR`。
- **B3**：去掉 `return ret;;` 多余的分号。

### 4.8 `thread.c`、`thread.h`、`sched/core.c`（P1–P3、Y3）

- `evl_thread.inband_work` 换成 `inband_wakeup`（`WAKE_TASK`）。新增 `inband_signals`（`CALL`）。两条记录的 cookie 都是 `task_struct`，在映射线程时由 `set_inband_notify_target()` 设置，位置在 `dovetail_init_altsched()` 之后、开启 dovetail 之前。
- `evl_switch_inband_details` 改为 post `inband_wakeup`。时序和原来一样：post 发生在关硬中断的 oob 上，合成中断要等 inband 恢复运行时才处理。
- **信号（Y3）**：
  - `evl_signal_thread` 不再 `evl_alloc`。改为把 `BIT(sig)` 累加进这个线程的固定记录；SIGDEBUG 的原因码用 `set_value` 记下。
  - inband 处理函数 `deliver_inband_signals` 取走全部位，逐个 `send_sig`/`send_sig_info`，只用副本里的 `task_struct`。
  - 从 inband 调用时也走 post，中断开着就同步投递。
- kthread：`irq_work` 换成 `done_notify`（`COMPLETE`）。`__evl_run_kthread` 在 `wait_for_completion` 之后再 drain。
- dispose：释放线程之前，drain `inband_wakeup` 和 `inband_signals`。
- 删去 `trace_evl_inband_wakeup` 的调用点：中继的处理函数里不再读可信线程，所以拿不到它要的参数。tracepoint 本身还在。

### 4.9 `syscall.c`（Y4）

- 删去 `cap_raised(current_cap(), CAP_SYS_NICE)`，只检查 `evl_current() != NULL`。
- 依据：attach 这个门会给每个线程加上 `CAP_SYS_NICE`（`add_u_cap`），所以"已经 attach"本身就是这个检查的可信副本。线程 attach 以后，Linux 侧凭据的变化不再影响 oob 系统调用。

### 4.10 `clock.c`（Y5）

- 重启日期改为保存在 `curr->restart_expires`。
- `restart_block.fn` 仍然由 EVL 写入，并在重启时读一次，用来判断 inband 期间有没有别的系统调用占用过重启块。这个值只决定是否返回 `-EINTR`，属于只影响调用者自身的输入（§1）。

### 4.11 `proxy.c`

跟随新接口：转发者的叫醒改成 `evl_init_inband_call(..., relay_kick, ...)`，删除头文件里的 `evl_proxy_inband_notify` 声明。其余部分和 doc 22 相同。

---

## 5. 不变式

| # | 不变式 | 由谁保证 |
|---|---|---|
| K1 | 可信代码在 oob 上从不调用 Linux 函数来通知 inband | 所有 Z → z 通知都经过 `evl_inband_notify_post`，它只操作可信内存，再调用 Dovetail 的 `irq_post_inband` |
| K2 | z 写不到通知记录 | z 只能经过门拿到副本；门在关硬中断时从可信链表取记录 |
| K3 | z 不投递，最坏后果是 inband 等待者不醒、内存晚释放、信号丢失或合并 | 记录大小固定，合并发送；可信堆没有按通知计的分配 |
| K4 | 记录和它指向的 inband 对象，要等中继放手以后才释放 | dispose 或 pass 时 drain；一次性记录在 `fn` 之后不再被访问 |
| K5 | oob 系统调用的放行与否不读 Linux 的可变状态 | §4.9 |

---

## 6. 行为变化

| 变化 | 影响 |
|---|---|
| post 之前已经挂起的信号会合并；SIGDEBUG 只保留最后一个原因码 | 和标准信号的语义一致。如果 HM 在 inband 处理之前连续报告多个原因，应用只能看到最后一个 |
| 线程 attach 以后，即使放弃 `CAP_SYS_NICE`，也不会失去 oob 系统调用的权限 | 原来会在下一次 oob 调用时返回 `-EPERM` |
| inband 上的可信代码改为 post + 同步投递，不再直接唤醒 | 在中断开着时和原来等价；关着时推迟到解除 stall |
| tracepoint `evl_inband_wakeup` 不再触发 | 只影响跟踪 |
| monitor 的 oob 写传入内核地址时返回 `-EFAULT` | 原来没有检查 |

---

## 7. 验证

| 项 | 结果 |
|---|---|
| 构建 | `make O=../build/linux-evl-qemu bzImage`，EVL 代码没有新的警告 |
| initramfs | 补上 `libm.so.6`、`libgcc_s.so.1`、`libatomic.so.1`，原来因为环境问题失败的 `fault`、`ring-spray`、`fpu-stress`、`hectic`、`observable-race`、`thread-demote` 现在都能运行 |
| **改动前（HEAD，同一个 initramfs）** | 78 个测试 74 个通过（77 个 libevl 测试加 `pipe-stress`）。失败：`ptrace-sync`（initramfs 里没有 gdb）、`sched-quota-accuracy`、`sched-tp-accuracy`、`sched-tp-overrun`（配置里没开对应的调度类） |
| **改动后** | 74/78，失败的 4 个相同。dmesg 里没有 WARNING、BUG、Oops |
| 新路径确实被走到 | 一轮测试里 `EVL in-band notify` 中断约 20 万次，主要来自 P1。`thread-demote`（信号）、`hectic`（stax）、`monitor-*-inband`、`observable-inband`、`poll-observable-inband`、`file-dup`/`poll-close`（crossing）、`clone-fork-exec`/`detach-self`（element 销毁、kthread）都通过 |

QEMU 命令（initramfs 在会话临时目录 `initramfs2.cpio.gz`）：

```
qemu-system-x86_64 -enable-kvm -cpu host -smp 4 -m 2G \
  -kernel build/linux-evl-qemu/arch/x86/boot/bzImage -initrd initramfs2.cpio.gz \
  -append "console=ttyS0 nokaslr" -nographic -no-reboot
```

---

## 8. 剩下的交集和下一步

| # | 内容 | 说明 |
|---|---|---|
| N1 | **可信句柄表**（Y7，doc 20） | oob 路径上最大的黄色交集。要改 libevl 的 ABI：clone 时返回句柄，公开 element 按名字 attach，`oob_*` 接受句柄；`poll.c`、`clock.c` 里按 fd 查找的地方一起改。测试里直接对 fd 调用 `oob_read`/`oob_write` 的代码也要改 |
| N2 | **把剩下的 Linux 对象移出可信对象**（Y2'） | stax 的 `inband_wait`、crossing 的 `passed`、`evl_work` 的 `wq_work`、element 的 `cdev`/`dev`/`devname`/`hash`/`flush`/`rcu`、`evl_thread.exited`、`evl_kthread.done`。和 doc 20 步骤 6（拆分 element）一起做 |
| N3 | 审计用户共享内存的每一处读（Y11） | monitor、mutex 的 sstate 和 fastlock。doc 17 的注释列出了全部读的位置 |
| N4 | oob 路径的 `raw_copy_*_user` 补上 `access_ok`（Y12） | 建议统一封装一个 `evl_copy_from_user()`，和 doc 22 的 N5 合并 |
| N5 | 信任基清单（doc 23） | Y8、Y9，以及中继依赖的 `irq_post_inband`、`sync_current_irq_stage` |
| N6 | 驱动和网络栈 | `drivers/evl/hectic.c` 还在 oob 上用 `irq_work`。`kernel/evl/net/` 在当前配置下不编译，它用到的 `evl_work`、crossing 接口没有变，但需要在打开 `CONFIG_EVL_NET` 时编译确认 |
| N7 | 中继的风暴和公平性 | 每个 CPU 一个合成中断；z 可以不处理，但 Z 侧只是挂链表，是 O(1) 的。合成中断太多会拖慢 inband，这和 doc 22 的 N3 是同一个问题 |
| N8 | 物理隔离 | 现在是逻辑划分。隔离以后，`CALL` 类型里属于"可信侧推迟工作"的函数要改成真正的门，不能再是 z 侧的函数指针 |

---

## 9. 文件清单

| 文件 | 改动 |
|---|---|
| `include/evl/inband_notify.h`、`kernel/evl/inband_notify.c` | 中继第二版 |
| `include/evl/monitor.h`、`kernel/evl/monitor.c` | P4、Y2、Y6 |
| `include/evl/observable.h`、`kernel/evl/observable.c` | P5、Y2 |
| `include/evl/stax.h`、`kernel/evl/stax.c` | P6 |
| `include/evl/work.h`、`kernel/evl/work.c` | P7 |
| `include/evl/crossing.h` | P8 |
| `include/evl/factory.h`、`kernel/evl/factory.c` | P9、B1–B3 |
| `include/evl/thread.h`、`kernel/evl/thread.c`、`kernel/evl/sched/core.c` | P1–P3、Y3 |
| `kernel/evl/syscall.c` | Y4 |
| `kernel/evl/clock.c` | Y5 |
| `kernel/evl/proxy.c` | 跟随新接口 |
| `kernel/evl/xbuf.c`、`init.c`、`Makefile` | doc 22 的改动，这次未变 |
