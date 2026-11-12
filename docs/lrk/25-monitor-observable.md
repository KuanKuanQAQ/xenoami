# 25 monitor 与 observable：功能、问题与修改

> 2026-11-12。本文和 doc 22（xbuf、proxy）是同一种写法：先讲组件是干什么的，再按功能流程列标记、代入 Z/z 找交集，最后讲怎么改的、还剩什么。可以单独阅读。
>
> 代码对照：
> - 原版：`~/xenoami/linux-evl-orig`（上游 `v6.12.y-cip-evl-rebase`，`5f5548b11fbd`，只读 worktree）
> - 改后：`~/xenoami/linux-evl`（工作区，未提交）
>
> 下文 `orig:` 前缀指原版，`now:` 前缀指改后。更细的逐字段、逐函数说明见 doc 16 的 `16-subsystems/monitor.md`、`observable.md`；本次改动在 doc 24 里的编号是 P4、P5、Y1、Y2、Y6。

---

## 0. 术语（摘自 doc 22）

| 术语 | 含义 |
|---|---|
| **Z** | 必须可信的闭包：实时域执行的代码、它依赖的数据、写这些数据的代码……递归下去 |
| **z** | 无法可信的闭包：纯非实时线程能触及的代码、它们能写的数据、依赖这些数据的代码……递归下去 |
| **交集** | Z∩z，要截断的地方 |
| **Z 受 z 影响** | Z 读 z 写的值、拿 z 可能持有的锁、执行 z 的代码、内存被 z 写、等 z 推进、从 z 的资源池分配 |
| **Z 通知 z** | Z 需要 z 做一件事（唤醒、释放……）。只能在 Z 的内存里留一条记录，由 z 来取，不能调用 z 的函数 |
| **门（gate）** | z 进入 Z 的入口函数，由 Z 实现，参数一律当作输入 |
| **输入 / 提示** | 来自 z 的值。输入要先校验；提示是取任何值都无害的值，只用来决定"要不要多做一次" |

明确放弃的：z 的活性（z 可以不唤醒、不释放），唤醒的精确性（可以合并、可以多余）。

---

## 1. 两个组件是干什么的

### 1.1 monitor：用户态同步原语的内核后端

libevl 的大部分同步原语，在内核里都是一个 monitor。monitor 有两大类：

| 类型 | 协议 | libevl 对应 | 语义 |
|---|---|---|---|
| **GATE**（门） | `EVL_GATE_PI` | `evl_mutex`（优先级继承） | 互斥锁 |
| | `EVL_GATE_PP` | `evl_mutex`（优先级天花板） | 互斥锁，持有者临时升到天花板优先级 |
| **EVENT**（事件） | `EVL_EVENT_GATED` | `evl_event` | 条件变量：持门等待，等待时释放门 |
| | `EVL_EVENT_COUNT` | `evl_sem` | 计数信号量 |
| | `EVL_EVENT_MASK` | `evl_flags` | 事件标志组：按位发送、按位等待 |

**快速路径在用户态。** 每个 monitor 有一块共享状态 `sstate`，放在 EVL 共享堆里，映射进用户进程。没有竞争时 libevl 直接在用户态对 `sstate` 做原子操作（抢锁、信号量减一），完全不进内核；只有需要睡眠或唤醒别人时才进内核。

**跨域使用。** COUNT 和 MASK 两种事件还可以被**普通 Linux 线程**操作：
- `read()`：等一个信号量 / 等一组标志位，在 Linux 调度器上睡眠；
- `write()`：发信号量 / 发标志位；
- `poll()`/`select()`/`epoll`：MASK 有位时 POLLIN；inband 读者把位收齐后 POLLOUT。

典型用法：实时线程每处理完一帧就 `evl_post_flags()`，一个普通的日志线程用 `epoll` 等这个 flags，收到后去写磁盘。实时侧发，Linux 侧等。

### 1.2 observable：发布 / 订阅通知通道

observable 是观察者模式：

- **订阅**：任何线程（EVL 的或普通 Linux 的）都可以订阅，订阅时指定自己最多积压多少条（backlog）。
- **发布**：一条通知 = 标签 `tag` + 值 `event`。发布者可以是 oob 上的实时线程、inband 上的普通线程，也可以是内核自己。
- **接收**：订阅者 `read`/`oob_read`，每条带序号、发布者 pid、时间戳。
- 选项：广播（默认）/ 单播轮转（做线程池）/ 只在变化时通知（ONCHANGE）。

**每个 EVL 线程本身就是一个 observable**：线程开启健康监测（HM）后，调度器在 oob 上发现异常（比如实时线程意外切到 inband）时，经 `evl_notify_thread()` → `evl_send_observable()` 往这个线程的 observable 里发一条通知。监控程序（通常是普通 Linux 线程）订阅它。这就是一条 oob 发、inband 收的路径。

对象结构：

```
evl_observable ── observers 链表 ── evl_observer（每个"线程 × observable"一个）
                                      ├─ backlog[]：预先分配好的通知记录
                                      └─ free_list / pending_list
task_struct ─(Dovetail 的 per-task 状态)─▶ evl_subscriber ── 红黑树，按 observable 句柄索引 observer
```

通知记录全部预先分配。发布时只是从 `free_list` 摘一条、填好、挂到 `pending_list`，oob 上不需要分配内存。

### 1.3 共同点：它们是 EVL 有意提供的跨域同步点

两者都允许**一边在 oob、一边在 inband**：

| 方向 | monitor | observable |
|---|---|---|
| oob 发、inband 等 | 实时线程 post，Linux 线程 `read`/`poll` 醒来 | 实时线程或 HM 发布，Linux 订阅者 `read`/`poll` 醒来 |
| inband 发、oob 等 | Linux 线程 `write`，实时线程醒来 | Linux 线程发布，实时订阅者醒来 |
| 反压（"有空间"） | MASK：inband 读者收齐位后 POLLOUT | 订阅者读走记录、backlog 有了空位后 POLLOUT |

Linux 线程只能睡在 Linux 的等待队列上，所以每个对象都要有 Linux 等待队列；oob 侧发生的事又要去唤醒它们。**这两件事就是本文的全部交集来源。**这个机制本身不能去掉，否则这两个原语就不能跨域使用了。

---

## 2. 流程与标记

### 2.1 monitor（EVENT，COUNT/MASK）

| 流程 | 步骤 |
|---|---|
| **oob 等**（`wait_count_oob` / `wait_mask_oob`） | 持 `wait_queue.wchan.lock`（硬锁）：试取（计数减一 / 取位）；取不到就挂到 EVL 等待队列，`evl_wait_schedule()` |
| **inband 等**（`__wait_monitor_inband` 宏） | 循环：持 **Linux 等待队列锁**，再持 `wchan.lock`，试取；取不到就把栈上的 `wq_entry` 挂到 **Linux 等待队列**，设 `TASK_INTERRUPTIBLE`，放两把锁，`schedule()` |
| **post**（`post_count` / `post_mask`，两个 stage 都可能） | 持 `wchan.lock`：改计数或位图，唤醒 oob 等待者；发 oob poll 事件；`evl_schedule()`；**最后唤醒 inband 等待者** |
| **inband poll**（`monitor_poll`，只支持 MASK） | `poll_wait()` 把 poll 表项挂到两个 Linux 等待队列上 |
| **POLLOUT**（`wait_monitor_inband` 的 MASK 分支） | inband 读者成功收齐位后，唤醒写侧等待队列 |

标记：

| 标记 | 放在哪里 | 谁写 | 谁读 |
|---|---|---|---|
| 计数 / 位图 `sstate->u.event.value` | 共享堆（用户可写） | 用户态快速路径、内核 post/wait | 同左 |
| EVL 等待队列 `wait_queue` | monitor（可信） | EVL 核心 | EVL 核心 |
| **Linux 等待队列头 `inband_wait_r/w`** | **orig：嵌在 monitor 里** | **Linux 的等待代码**（`add_wait_queue`、`poll_wait`、`finish_wait`），以及 inband 等待宏 | post 用 `waitqueue_active` 偷看；`wake_up` 遍历 |
| 等待项 `wq_entry` / poll 表项 | **inband 线程的栈 / Linux 的堆** | Linux | `wake_up` 调用其中的 `func` |
| **irq_work `inband_wake_r/w`** | **orig：嵌在 monitor 里** | Linux 的 irq_work 核心（flags、链表节点） | 同左 |
| element 引用计数 | monitor（可信） | oob 上 get；**irq_work 回调里 put** | factory |

### 2.2 observable

| 流程 | 步骤 |
|---|---|
| **发布**（`push_notification` → `wake_up_observers`） | 持 `observable->lock` 分配序号；对每个订阅者在 `oob_wait.wchan.lock` 下取空闲记录、填好、挂 pending；遍历中途被取消的订阅者放进 `flush_list`；然后唤醒 oob 读者、发 POLLIN、**唤醒 inband 读者**；有 `flush_list` 的话 **在 inband 上释放它们** |
| **oob 读**（`pull_from_oob`） | EVL 等待队列 |
| **inband 读**（`pull_from_inband`） | 先持 **Linux 等待队列锁**，再持 `oob_wait.wchan.lock`；没有记录就挂 `wq_entry`，`schedule()` |
| **读完**（`pull_notification`） | 记录放回 `free_list`；订阅者从"满"变"可写"时发 POLLOUT、**唤醒 inband 写侧** |
| **订阅**（`add_subscription`，inband） | 挂上 observer，可写数加一，**唤醒 inband 写侧** |
| **释放被丢弃的订阅者**（`do_flush_work`） | 遍历 `flush_list`，`kfree` |

标记和 monitor 一样多出三样：Linux 等待队列头 ×2、irq_work ×3（读、写、flush），以及 irq_work 回调里的 `evl_put_element`。

---

## 3. 原实现里的交集

### 3.1 一览

| # | 交集 | 在哪 | 类别 | doc 24 编号 |
|---|---|---|---|---|
| X1 | Linux 等待队列头嵌在可信对象里 | `orig: include/evl/monitor.h:34-37`、`orig: include/evl/observable.h:27-28` | Z 的内存被 z 写；Z 要拿 z 的锁 | Y2 |
| X2 | 在 inband 上直接 `wake_up`，沿 z 的链表执行 z 的函数 | `orig: monitor.c:402-405`、`:849-850`；`orig: observable.c:198-199`、`:654-661`、`:888-890` | Z 执行 z 的代码 | P4、P5 |
| X3 | 在 oob 上 `irq_work_queue`，irq_work 嵌在可信对象里 | `orig: monitor.c:406-409`、`:597-601`；`orig: observable.c:631`、`:664`、`:893` | Z 调用 z 的代码；Z 的内存被 z 写 | P4、P5、Y1 |
| X4 | irq_work 回调里 `evl_put_element` | `orig: monitor.c:415-430`；`orig: observable.c:350-365`、`:438-444` | z 的入口写可信引用计数；最后一个引用可能在中断上下文里放掉 | P4、P5 |
| X5 | `flush_irqwork` 回调做大量可信操作 | `orig: observable.c:438`（`do_flush_work` 遍历 `flush_list`、`kfree`） | z 决定何时、是否执行一段可信代码；入口是 z 给的 `irq_work *` | P5 |
| X6 | `monitor_common_write` 用 `copy_from_user`，而 `oob_write` 也走它 | `orig: monitor.c:1129-1145` | Z 在 oob 上执行带 inband 检查的代码（`might_fault`） | Y6 |
| X7 | `waitqueue_active()` 读 Linux 等待队列头 | X2、X3 的每一处 | Z 读 z 写的值 | — |
| X8 | inband 等待路径：先拿 Linux 锁，再拿可信硬锁 | `orig: monitor.c:787-797`、`orig: observable.c:792-829` | 混合代码：同一段代码既改 Z 状态、又改 z 链表 | 未编号，见 §7 |
| X9 | 经 fd 找对象、读 `filp->f_flags` | 所有入口 | Z 读 z 的值 | Y7 |
| X10 | `sstate` 在共享堆里，用户可写 | monitor 全部 | 用户输入 | Y11 |
| X11 | `evl_subscriber` 指针放在 `task_struct` 里 | `evl_get_subscriber()` | Z 的根指针放在 z 的对象里 | Y8 一类 |

### 3.2 逐个解释

**X1：等待队列头不是指针，是一个嵌入的结构体。**

```c
struct wait_queue_head { spinlock_t lock; struct list_head head; };
```

可信对象里放着的是一把 Linux 自旋锁和链表头的 `next`/`prev`。链表上的节点全在 z：`__wait_monitor_inband` 的 `wq_entry` 是 inband 线程栈上的局部变量，poll 的表项是 Linux `poll_wqueues` 分配的。所以 Linux 的 `add_wait_queue`、`poll_wait`、poll 注销都会改这把锁和这两个指针：**Z 的内存被 z 写**。

**X2：直接 `wake_up` 等于执行 z 的代码。** `wake_up()` 要先拿 `wq->lock`（可能正被某个 Linux 线程持有），再沿着 z 的链表逐个调用节点里的 `func` 函数指针（poll 的是 `pollwake`）。也就是可信代码顺着 z 给的指针执行 z 指定的函数。原实现在 inband 上走这条路，在 oob 上走 X3。

**X3：irq_work。** `irq_work_queue()` 是 Linux 的代码，会写 Linux 的 per-CPU 链表；嵌在可信对象里的 `struct irq_work` 的 flags 和链表节点，又由 Linux 的 `irq_work_run` 改写。monitor 原版里 `inband_wake_w` 初始化了但从没被排过，是死代码。

**X4：引用计数由 z 决定何时放掉。** 原版每次排 irq_work 前先 `evl_get_element`，回调里 `wake_up` 之后 `evl_put_element`。问题有两个：
- `irq_work_queue` 的返回值决定要不要立刻 put，而它的语义由 Linux 的 irq_work 标志决定；
- 如果回调里放的恰好是最后一个引用，对象就会在中断上下文里开始销毁。

**X5：flush 是一段可信操作，入口却是 Linux 的 irq_work 回调。** 发布遍历时，有订阅者被并发取消的话，它被放进 `flush_list`，要到 inband 才能 `kfree`。原版用 `flush_irqwork` 推迟，回调 `container_of` 出 observable，然后遍历可信链表、释放可信内存、放引用。

**X6：** `copy_from_user()` 里有 `might_fault()`，这是 inband 的检查代码，doc 17 §5.1 发现的冲突。

**X7：偷看。** `waitqueue_active()` 只是比较 `head.next == &head`，读一个字，不往下解引用。

---

## 4. 截断方法（摘自 doc 22 §5）

| 方法 | 做法 |
|---|---|
| 私有副本 | Z 保存自己的一份，只信自己的 |
| 输入化 | z 的值只经门传入、校验后用；或者只当提示用 |
| 只记录、不调用 | Z 只在自己的内存里留记录、发合成中断，z 自己来取 |
| 归属单侧 | 原本两域共用、实际上只属于一侧的东西，分给那一侧 |
| 消除 | 改流程，让依赖消失 |

本次用到：X1 归属单侧；X2、X3、X5 只记录；X4 消除；X6 消除（改用不带 inband 检查的版本）；X7 输入化（只当提示）。

---

## 5. 怎么改的

### 5.1 等待队列头分给 z（X1，归属单侧）

**now: `kernel/evl/monitor.c:27-30`**、**now: `kernel/evl/observable.c:26-29`**：

```c
struct evl_monitor_ib {             /* observable 里叫 evl_observable_ib，内容相同 */
	wait_queue_head_t wait_r;
	wait_queue_head_t wait_w;
};
```

- 创建时单独 `kzalloc`。monitor 只给 EVENT 类型分配（`now: monitor.c:1302-1308`），GATE 类型没有 inband 等待者；observable 在 `evl_alloc_observable` 里分配（`now: observable.c:1094-1098`），线程自带的 observable 也走这里。
- 可信对象里只剩一个指针 `ib`，创建时写一次，之后不变（`now: include/evl/monitor.h:36-43`、`now: include/evl/observable.h:29-35`）。
- Linux 爱怎么改这两个等待队列都行，改到的是 z 自己的内存。
- inband 等待宏、`pull_from_inband`、`poll_wait` 只是把 `&mon->inband_wait_r` 换成 `&mon->ib->wait_r`，逻辑不变。

和 doc 22 的 xbuf、proxy 的 `iend` 是同一种做法。

### 5.2 唤醒改成只记录（X2、X3，P4、P5）

可信对象里新增两条通知记录（`now: monitor.h:42-43`、`now: observable.h:31-35`）：

```c
struct evl_inband_notify notify_r;   /* type = WAKE_UP，cookie = &ib->wait_r */
struct evl_inband_notify notify_w;   /* type = WAKE_UP，cookie = &ib->wait_w */
```

所有唤醒 inband 等待者的地方，统一换成两个辅助函数（`now: monitor.c:38-50`，observable 同名同义）：

```c
static void notify_inband_readers(struct evl_monitor *mon)
{
	smp_mb();	/* 与等待方 set_current_state() 里的屏障配对 */
	if (waitqueue_active(&mon->ib->wait_r))     /* 提示 */
		evl_inband_notify_post(&mon->notify_r); /* 只记录 */
}
```

对照：

```c
/* orig: monitor.c:400-411（post_count 末尾） */
smp_mb();
if (waitqueue_active(&mon->inband_wait_r)) {
	if (running_inband()) {
		wake_up(&mon->inband_wait_r);                 /* X2 */
	} else {
		evl_get_element(&mon->element);               /* X4 */
		if (!irq_work_queue(&mon->inband_wake_r))     /* X3 */
			evl_put_element(&mon->element);
	}
}

/* now: monitor.c:430 */
notify_inband_readers(mon);
```

`evl_inband_notify_post()` 做的事（`now: kernel/evl/inband_notify.c:24-49`）：
1. 原子置位记录里的 `PENDING`，已经置位就直接返回（合并）；
2. 关硬中断，把记录挂到**可信的** per-CPU 链表，`irq_post_inband()` 发一个合成中断（Dovetail，信任基）；
3. 如果当前就在 inband 且中断开着，立刻同步中断日志，所以 inband 上的 post 在返回前已经投递完。

inband 一侧的中断处理函数经门取走一份**副本**，然后自己 `wake_up(cookie)`。可信代码不再拿 Linux 的锁，不再沿 z 的链表执行代码，也不再写任何 Linux 数据。

替换的位置：

| 文件 | 位置 | 原来 | 现在 |
|---|---|---|---|
| monitor | `post_count` 末尾 | X2/X3 | `notify_inband_readers` |
| monitor | `post_mask` 末尾（还有剩余位时） | X2/X3 | `notify_inband_readers` |
| monitor | `wait_monitor_inband` MASK 分支（收齐位后 POLLOUT） | 直接 `wake_up(wait_w)` | `notify_inband_writers` |
| observable | `add_subscription`（订阅后可写） | 直接 `wake_up(wait_w)` | `notify_inband_writers` |
| observable | `wake_up_observers`（发布后） | X2/X3 | `notify_inband_readers` |
| observable | `pull_notification`（读后恢复可写） | X2/X3 | `notify_inband_writers` |

**inband 路径为什么也改。** 这几处在 inband 上执行时，调用者本身就是 z 线程，直接 `wake_up` 看起来无害。但统一成"可信代码一律只记录"以后，"可信代码里不出现 `wake_up`"就可以机械地检查（grep），不必逐处论证调用者是谁。代价只是多一次合成中断，在中断开着时是同步投递，语义不变。

**X7，偷看留着，但只当提示。** `waitqueue_active()` 仍然读 z 的一个字。它取错值的后果：
- 错读成"有人等"：多发一次通知，z 那边 `wake_up` 一个空队列，无害；
- 错读成"没人等"：inband 等待者漏掉一次唤醒，损失的是 z 的活性，按约定可以接受。

doc 16 曾建议改成 EVL 自己维护的"inband 等待者计数"。这里没有这样做：计数要由 inband 等待路径增减，在 X8 没解决之前它同样是 z 写的值，换了并不更可信。

### 5.3 flush 改为一条 CALL 记录（X5）

```c
/* now: observable.c:452-455 */
static void inband_flush_call(const struct evl_notify_event *ev)
{
	do_flush_work(ev->cookie);
}
/* 初始化（now: observable.c:1118-1119） */
evl_init_inband_call(&observable->flush_call, inband_flush_call, observable, 0);
/* push_notification 末尾（now: observable.c:636-640） */
if (running_inband())
	do_flush_work(observable);
else
	evl_inband_notify_post(&observable->flush_call);
```

- `fn` 和 `cookie` 都在可信记录里，创建时写定；z 只决定**什么时候**调用，不能决定调用谁、传什么。
- `do_flush_work` 在任何时刻执行都是安全的：它只在 `observable->lock` 下摘 `flush_list` 的节点并释放。z 推迟或不执行，后果只是这些 observer 的内存晚释放（K3 的"内存晚释放"）。
- 这是一个"推迟到 inband 执行的门"。现在它由中继直接调用；做物理隔离时，它要变成正式的 z → Z 入口。

### 5.4 生命周期：去掉每次通知的引用，改为销毁前 drain（X4，消除）

- 唤醒时不再 `evl_get_element`/`evl_put_element`。
- 依据：所有 post 都发生在调用者持有引用的时候（经 fd 的文件操作、经句柄的 `get_monitor_by_fundle`、线程持有自己的 observable）。所以最后一个引用放掉、dispose 开始以后，不会再有新的 post。
- dispose 先等中继放手，再释放 `ib`（`now: monitor.c:1396-1403`、`now: observable.c:1148-1152`）：

```c
evl_inband_notify_drain(&mon->notify_r);
evl_inband_notify_drain(&mon->notify_w);
kfree(mon->ib);
```

`drain` 等记录的 `PENDING` 和 `BUSY` 都清掉，这时中继既不会再取这条记录，也不在用它的 cookie（也就是 `ib` 里的等待队列）。顺带消除了"最后一个引用在中断上下文里放掉"的隐患。

### 5.5 `monitor_common_write`（X6）

```c
/* orig: monitor.c:1144 */
ret = copy_from_user(&val, u_buf, sizeof(val));

/* now: monitor.c:1135-1138 */
if (!access_ok(u_buf, sizeof(val)) ||
	raw_copy_from_user(&val, u_buf, sizeof(val)))
	return -EFAULT;
```

`raw_copy_from_user` 属于体系结构代码（信任基）。`access_ok` 保证用户传进来的不是内核地址；原来的 `copy_from_user` 里也有这个检查，换成 raw 版本后要自己补上。

### 5.6 删掉的东西

- monitor：`inband_wake_r_irqwork`、`inband_wake_w_irqwork`（后者原本就是死代码）、`#include <linux/irq_work.h>`；
- observable：`inband_wake_r_irqwork`、`inband_wake_w_irqwork`、`inband_flush_irqwork`；
- 两边的 `init_irq_work` 和每次唤醒时的 get/put。

---

## 6. 改完后的不变式

| 编号 | 不变式 | 由什么保证 |
|---|---|---|
| M1 | 可信对象里没有 Linux 会写的成员（等待队列头、irq_work） | 5.1、5.6。**例外**：`element` 里的 `cdev`、`rcu` 等，见 §7 |
| M2 | 可信代码不调用 `wake_up`、`irq_work_queue`，不拿 Linux 等待队列的锁去唤醒 | 5.2，统一走 `evl_inband_notify_post` |
| M3 | 可信代码读 z 内存只有 `waitqueue_active` 一处，结果只当提示 | 5.2 |
| M4 | z 能决定的只有"什么时候投递、投不投递"，不能决定唤醒谁、调用谁 | 记录的 `type`、`cookie`、`fn` 在可信内存里，创建时写定；z 只拿副本 |
| M5 | z 不投递的最坏后果：inband 等待者不醒、被丢弃的 observer 晚释放 | 记录大小固定、合并；没有按次的分配 |
| M6 | `ib` 和记录要等中继放手后才释放 | dispose 先 drain |

---

## 7. 还没解决的

### 7.1 inband 等待路径是混合代码（X8）

`__wait_monitor_inband`（`now: monitor.c:745-818`）和 `pull_from_inband`（`now: observable.c:770-825`）是 EVL 的代码，在 inband 上替普通线程执行：

```
spin_lock(ib->wait_r.lock)          ← z 的锁
  raw_spin_lock(wchan.lock)         ← Z 的硬锁
    trywait（改 Z 的计数 / 记录链表）
    __add_wait_queue(ib->wait_r, …) ← 在持有 Z 锁时写 z 的链表
    set_current_state
  raw_spin_unlock(wchan.lock)
spin_unlock(ib->wait_r.lock)
schedule()
```

问题：
- 同一段代码既改 Z 的状态，又改 z 的链表，分不出它是 Z 还是 z；
- 持有可信硬锁时，顺着 z 内存里的指针写（`__add_wait_queue` 会写 `head->next->prev`）。逻辑划分下这不算新增风险，因为 z 本来就能写任何地方；但到物理隔离（路线图 P8）时，这段代码必须拆开。

两把锁嵌套的目的，只是不让 post 和"挂上等待队列"之间丢唤醒。Linux 的标准写法不需要嵌套：

```
等待方（z）：                         唤醒方（Z）：
  prepare_to_wait(wq, …)                改计数 / 位图（在 Z 锁下）
  if (gate_trywait(mon)) break;         smp_mb()
  schedule()                            if (waitqueue_active(wq)) post
```

`prepare_to_wait` 里的屏障与唤醒方的 `smp_mb` 配对，保证"等待方看到条件已满足"和"唤醒方看到有人在等"至少有一个成立。`gate_trywait` 是一个只接受数值的门，在 Z 锁下完成试取后放锁返回。这样 inband 等待路径变成纯 z 代码加一次门调用，和 doc 22 里 xbuf 不可信端的做法一致。

改动不大，但要同时改 COUNT、MASK、observable 三处，并重新跑 `monitor-count-inband`、`monitor-flags-inband`、`observable-inband`、`poll-observable-inband`。建议作为下一步。

### 7.2 其他

| 问题 | 说明 | 归到 |
|---|---|---|
| 经 fd 找对象、读 `filp->f_flags`（X9） | oob 入口经 Linux 的 fd 表、`private_data` 找到 monitor/observable | doc 24 N1：句柄表 |
| `sstate` 在共享堆里（X10） | 计数、位图、`pollrefs`、条件变量标志都由用户态直接写。EVL 已经把 `type`/`protocol`/`gate_offset` 当作不可信、只信内核副本；剩下的要逐处确认只当输入用。另外整个共享堆映射给所有打开 `/dev/evl/control` 的进程，别的进程也能改 | doc 24 N3 |
| `evl_subscriber` 指针在 `task_struct` 里（X11） | 和 `evl_current()` 经 Dovetail per-task 状态是同一个问题 | 信任基（doc 23） |
| `element` 里的 `cdev`、`rcu` 等 | 所有 element 共有 | doc 24 N2，doc 20 拆分 element |
| 可信对象由 Linux 的 `kzalloc` 分配、`kfree` 释放 | monitor、observable、observer 都是 | 路线图 P2（内存） |
| `task_pid_nr(current)` 写进通知的 `issuer` | 只透传给读者，EVL 不据此判断 | 不透明数据，可接受 |
| `backlog_count` 没有上限 | 只在 inband 上 `kzalloc`，耗的是 Linux 的内存 | 不影响 Z |

---

## 8. 行为变化

| 变化 | 影响 |
|---|---|
| 多次 post 在投递前合并成一次 | 无。monitor 和 observable 的 inband 等待项都是非独占的（`init_wait_entry(&wq_entry, 0)`），poll 表项也是，`wake_up` 一次就唤醒全部 |
| inband 上的唤醒经合成中断同步投递 | 中断开着时，post 返回前已经投递，时序和原来的直接 `wake_up` 相同 |
| dispose 会等中继投递完 | 原来是 pending 的 irq_work 持引用、推迟 dispose；现在 dispose 主动等。正常情况下等待时间是一次中断投递 |
| `monitor_oob_write`/`write` 传内核地址返回 `-EFAULT` | 原来 `copy_from_user` 也会拒绝，行为相同；只是 oob 上不再执行 `might_fault` |

---

## 9. 验证

QEMU + libevl 测试套件，同一个 initramfs 下改前、改后都是 74/78，4 个失败相同且与本文无关（见 doc 24 §7）。与本文相关的测试全部通过：

| 覆盖 | 测试 |
|---|---|
| monitor 的 oob 路径 | `monitor-event`、`monitor-event-sigrel`、`monitor-event-targeted`、`monitor-event-untrack`、`monitor-flags`、`monitor-flags-broadcast`、`monitor-wait-multiple`、`monitor-wait-requeue`、`sem-*`（4 个）、GATE 类的 `monitor-pi-*`、`monitor-pp-*` 等 |
| monitor 的 inband 路径（本文改动的重点） | `monitor-count-inband`、`monitor-flags-inband`、`poll-flags`、`poll-sem` |
| observable | `observable-oob`、`observable-inband`、`observable-onchange`、`observable-unicast`、`observable-race`、`observable-thread`、`observable-hm`（HM 在 oob 上发布） |
| observable + poll | `poll-observable-inband`、`poll-observable-oob` |

一轮测试中中继的合成中断约 20 万次，无 WARNING。日志：scratchpad 的 `qemu-core3.log`。

---

## 10. 代码位置索引

| 内容 | orig | now |
|---|---|---|
| monitor 结构体 | `include/evl/monitor.h:20-49` | `include/evl/monitor.h`（`ib`、`notify_r/w`） |
| monitor inband 侧结构 | — | `kernel/evl/monitor.c:27-30` |
| 唤醒辅助函数 | — | `kernel/evl/monitor.c:38-50` |
| `post_count` | `kernel/evl/monitor.c:353` | `:382` |
| `post_mask` | `:543` | `:545` |
| inband 等待宏 | `:751` | `:745` |
| `monitor_common_write` | `:1129` | `:1122` |
| `monitor_factory_build` / `dispose` | `:1251` / `:1378` | `:1245` / `:1386` |
| observable 结构体 | `include/evl/observable.h:22-37` | `include/evl/observable.h` |
| observable inband 侧结构、辅助函数 | — | `kernel/evl/observable.c:26-48` |
| `add_subscription` | `kernel/evl/observable.c:112` | `:141` |
| flush | `inband_flush_irqwork` `:438` | `inband_flush_call` `:452` |
| `push_notification` / `wake_up_observers` | `:599` / `:639` | `:609` / `:646` |
| `pull_from_inband` / `pull_notification` | `:777` / `:834` | `:770` / `:827` |
| `evl_alloc_observable` / `dispose` | `:1100` / — | `:1084` / `:1143` |
| 通知中继 | — | `kernel/evl/inband_notify.c`、`include/evl/inband_notify.h` |
