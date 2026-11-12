# 27 Z → z 通知机制：原来怎么做、现在怎么做、为什么不会弄坏 Linux

本文可以单独阅读，回答三个问题：

1. 原版 EVL 在哪些地方、用什么办法通知 Linux（z）；
2. 现在的通知中继是怎么工作的，"多次 post 只投递一次"会不会让正常的 Linux 出错；
3. doc 25 §7.1 说的"inband 等待路径把 Linux 锁套在 Z 硬锁外面"到底是什么情况。

代码位置写法：`orig:` 指原版只读副本 `~/xenoami/linux-evl-orig`（上游 `v6.12.y-cip-evl-rebase`），`now:` 指当前的 `~/xenoami/linux-evl`。

---

## 0. 结论先行

- **原版本来就不能直接调用 Linux。** oob 阶段上不能调用 `wake_up`、`complete` 这类 Linux 函数，因为它们用的是 Linux 的普通自旋锁，Dovetail 下这种锁只屏蔽"虚拟中断"，挡不住 oob。所以原版 EVL 一直是"先登记，回到 inband 再做"，登记工具就是 Linux 的 `irq_work`。
- **现在只是把登记工具换了。** `irq_work` 的记录、链表和 `irq_work_queue()` 都属于 Linux（z）。现在换成 EVL 自己的通知中继，记录、链表和 post 函数都在 Z 里。真正的 `wake_up` 等动作仍然由 inband 来做，和原来一样。
- **合并不是新行为。** `irq_work_queue()` 对一个已经在排队的 irq_work 会直接返回 false，不会再排一次，所以原版"多次 queue 只执行一次"的语义本来就存在。中继完全照搬了 `irq_work` 的 PENDING/BUSY 两位协议，合并规则、清位时机和投递时机都和原来一致（§4）。
- **语义变化只有三处，都对正常 Linux 无害**（§4.4）：
  - 信号从"每次分配一个结构"改为"按信号号累积"；
  - monitor 和 observable 从"每次通知取引用"改为"销毁前 drain"；
  - inband 上调用时，从"直接调用 Linux 函数"改为"post 并当场投递"。在"inband 虚拟关中断"时，投递会推迟到开中断。
- **"z 可以不投递"只是威胁模型里的说法。** 意思是恶意的 z 不投递时，只伤到 z 自己。正常 Linux 一定会执行这个中断处理函数，所以活性和原版相同。
- **问题 3 不归通知机制管。** 通知是出向（Z → z）。锁嵌套出现在入向：普通 Linux 线程要在 EVL 对象上睡眠时，跑的是一段 EVL 写的 inband 代码，它先拿 Linux 等待队列的锁，再拿 Z 的硬锁。monitor、observable、stax 三处都有这个写法（§6）。

---

## 1. 为什么 Z 需要通知 z

EVL 有几类对象，Linux 线程和 EVL 线程都能用。典型场景：

```
Linux 线程 L（inband）                 EVL 线程 E（oob）
read(monitor_fd)  → 计数为 0，睡在 Linux 等待队列上
                                        oob_write(monitor_fd) → 计数 +1
                                        → 需要叫醒 L
```

E 在 oob 上，L 睡在 Linux 的等待队列里，叫醒 L 要调用 `wake_up()`。可是 `wake_up()` 要拿等待队列的 `spinlock_t`。在 Dovetail 下，`spin_lock_irqsave` 只做"虚拟关中断"（stall inband 阶段），oob 照样可以抢占持锁的 inband 代码。如果 E 在 oob 上也去拿这把锁，就可能和被它抢占的持锁者死锁，而且 Linux 的锁本身不是有界延迟的。

所以 **oob 上不能调用 Linux 函数**。这是 Dovetail 的基本规则，和我们的 Z/z 划分无关，原版也必须遵守。原版的办法是：

> oob 上只"登记一个待办"，然后发一个 inband 中断；等 inband 阶段恢复运行、处理这个中断时，再由 inband 代码执行 `wake_up()`。

这里"登记待办"用的就是 Linux 的 `irq_work`。

---

## 2. 原版：`irq_work` 是怎么工作的

### 2.1 数据结构

```c
struct irq_work {
	struct __call_single_node node;   /* llist 节点 + a_flags */
	void (*func)(struct irq_work *);
	struct rcuwait irqwait;
};
```

`a_flags` 里有两个关键位：

| 位 | 含义 |
|---|---|
| `IRQ_WORK_PENDING` | 已经排队，还没开始执行 |
| `IRQ_WORK_BUSY` | 已经被认领（从排队到执行完毕都置位） |

这个结构**嵌在 EVL 对象里**（比如 `struct evl_monitor` 里有 `struct irq_work inband_wake_r`），但它的字段由 Linux 的 `irq_work_queue`、`irq_work_run` 来读写，节点挂在 Linux 的 per-CPU 链表 `raised_list` 上。

### 2.2 排队：`irq_work_queue()`（`orig: kernel/irq_work.c`）

```c
static bool irq_work_claim(struct irq_work *work)
{
	oflags = atomic_fetch_or(IRQ_WORK_CLAIMED | ..., &work->node.a_flags);
	if (oflags & IRQ_WORK_PENDING)
		return false;            /* ← 已经在排队：什么都不做，直接返回 */
	return true;
}

bool irq_work_queue(struct irq_work *work)
{
	if (!irq_work_claim(work))
		return false;            /* ← 合并发生在这里 */
	llist_add(&work->node.llist, this_cpu_ptr(&raised_list));
	irq_local_work_raise();      /* 发中断 */
	return true;
}
```

Dovetail 改写了 `irq_local_work_raise()`（`orig: kernel/irq/pipeline.c:1715`）：

```c
void irq_local_work_raise(void)
{
	flags = hard_local_irq_save();
	irq_post_inband(inband_work_sirq);          /* 在 inband 中断日志里登记 */
	if (running_inband() &&
	    !hard_irqs_disabled_flags(flags) && !irqs_disabled())
		sync_current_irq_stage();               /* 若本来就在 inband 且开中断，立刻处理 */
	hard_local_irq_restore(flags);
}
```

### 2.3 执行：`irq_work_single()`

```c
flags = atomic_read(&work->node.a_flags);
flags &= ~IRQ_WORK_PENDING;
atomic_set(&work->node.a_flags, flags);   /* ① 先清 PENDING：此后再 queue 会重新排队 */
smp_mb();
work->func(work);                         /* ② 再执行 */
atomic_cmpxchg(..., flags & ~IRQ_WORK_BUSY); /* ③ 最后清 BUSY */
```

记住两点，§4 要用：

- **合并**：在 ① 之前的多次 queue，只会让 func 执行一次。
- **不丢**：① 之后到来的 queue 会看到 PENDING 已清，于是重新排队，func 会再执行一次。

---

## 3. 原版所有的 Z → z 通知

### 3.1 一览

原版 EVL 核心里，Z 通知 z 有两种直接形式：`irq_work_queue`，以及建在它之上的 `evl_call_inband`（`irq_work` → `queue_work`，多一跳）。另外还有 inband 上的直接调用（§3.3）和几种非 irq_work 的机制（§3.4）。

| 编号 | 场景 | `orig` 位置 | irq_work 的 func 做什么 | 现在的记录类型 |
|---|---|---|---|---|
| P1 | 线程从 oob 切回 inband | `sched/core.c:1130` | `inband_task_wakeup`（`thread.c:810`）：`wake_up_process(p)` | `WAKE_TASK` |
| P2 | EVL 内核线程启动完成 | `thread.c:455-456` | `wakeup_kthread_parent`：`complete(&kthread->done)` | `COMPLETE` |
| P3 | 给 EVL 线程发 Linux 信号 | `thread.c:1452-1459` | `sig_irqwork`：`send_sig_info`/`send_sig`，然后 put 引用、`evl_free_rcu` | `CALL` + 累积位 |
| P4 | monitor：唤醒 inband 读者 | `monitor.c:407`、`599` | `inband_wake_r_irqwork`：`wake_up` + put 引用 | `WAKE_UP` |
| P5 | observable：唤醒读者、写者，释放观察者 | `observable.c:664`、`893`、`631` | `wake_up` ×2；`do_flush_work`；都 put 引用 | `WAKE_UP` ×2，`CALL` |
| P6 | stax：oob 解锁后唤醒 inband 等待者 | `stax.c:345` | `wakeup_inband_waiters`：`wake_up_all` | `WAKE_ALL` |
| P7 | `evl_call_inband`：在 inband 执行一个函数 | `work.c:101` | `__evl_do_irq_work`：`queue_work`，失败则 put | `CALL` |
| P8 | crossing：最后一个 oob 用户离开 | `crossing.h:70` | `evl_open_crossing`：`complete(&c->passed)` | `COMPLETE` |
| P9 | element 的最后一次 put 发生在 oob 上 | `factory.c:286-287` | `do_put_element_irq`：挂到 flusher 队列并唤醒 | `CALL` + 一次性 |
| X-A | xbuf：oob 写入后叫醒 inband 读者 | `xbuf.c:348` | `resume_inband_reader`：`wake_up(&ibnd.i_event)` | `WAKE_UP` |
| X-B | xbuf：oob 读走后叫醒 inband 写者 | `xbuf.c:492` | `resume_inband_writer`：`wake_up(&obnd.o_event)` | `WAKE_UP` |
| PX | proxy：叫 inband 的 relay 工作搬数据 | `proxy.c:223`、`441`、`479`，经 P7 | `queue_work(relay_work)` | `CALL`（`relay_kick`） |

不在核心范围内、目前没改的：

| 位置 | 说明 |
|---|---|
| `net/output.c:282`、`net/{timestamping,socket,skb}.c`、`lib/cache.c` 的 `evl_call_inband` | 网络栈。后几处走 P7，已经自动换成中继；`output.c` 自己的 `irq_work` 还在。当前配置不编译 `CONFIG_EVL_NET`（doc 24 N6） |
| `drivers/evl/hectic.c:92,137,181` | 测试驱动，`irq_work` → `up(&nrt_synch)`，没改 |

### 3.2 逐个说明

下面每一项都按同一个格式写：**谁在等、谁来叫、func 做什么、合并后会不会丢东西、现在怎么做**。"合并后会不会丢东西"的分析见 §4，这里只给结论。

#### P1 线程切回 inband（`orig: sched/core.c:1120-1135`，`thread.c:810-819`）

- **背景。** EVL 线程就是一个 Linux 任务，它有两个调度器（doc 26 §3）。从 oob 切回 inband 时，EVL 调度器让出 CPU，Linux 调度器要把同一个 `task_struct` 唤醒，让它在 inband 上继续跑。
- **原版。** `evl_switch_inband` 在硬关中断下 `irq_work_queue(&curr->inband_work)`，然后 EVL 调度走。inband 处理中断时执行 `wake_up_process(p)`，Linux 调度器把它调度回来（`sched/core.c:867` 的注释画了这条链）。
- **合并。** 同一线程在被唤醒并真正回到 inband 之前，不可能再切一次 inband，所以实际上不会合并。即使合并，`wake_up_process` 也是幂等的。
- **现在。** `evl_inband_notify_post(&curr->inband_wakeup)`，记录类型 `WAKE_TASK`，cookie 是 `task_struct`（`now: sched/core.c:1131`）。

#### P2 内核线程启动（`orig: thread.c:415-460`）

- **背景。** `evl_run_kthread` 在 inband 上创建一个 Linux kthread，然后 `wait_for_completion(&kthread->done)`。新线程切到 oob、进入 dormant 之前，要告诉创建者"我好了"。
- **原版。** 新线程已在 oob 上，所以用 `irq_work` 去 `complete`。
- **合并。** 一次性，不会合并。
- **现在。** 记录类型 `COMPLETE`。创建者在 `wait_for_completion` 之后调用 `evl_inband_notify_drain`，确保中继已经放手，再继续使用或释放 kthread 结构（`now: thread.c:461-463`、`536`）。

#### P3 发信号（`orig: thread.c:1405-1460`）

- **背景。** 两个来源：
  - 健康监视（HM）发 `SIGDEBUG`（就是 `SIGXCPU`），`si_int` 里带原因码，例如"在 oob 上调了 Linux 系统调用"或"看门狗触发"；
  - 取消线程时发 `SIGTERM`（`thread.c:1044`）。
- **原版。** 在 inband 上直接 `do_inband_signal`。在 oob 上则**每次 `evl_alloc` 一个 `sig_irqwork_data`**，里面带一个专用的 irq_work，再取线程的引用，func 发完信号后 put 引用并释放这块内存。
- **原版的问题。** 每个信号都从可信堆分配。如果 z 一直不处理中断，Z 的堆会被耗尽（资源依赖，doc 26 §4）。
- **合并。** 原版每个信号都有自己的 irq_work，在 irq_work 这一层没有合并。现在按信号号合并，是**真正的语义变化**，分析见 §4.4。
- **现在。** 每个线程一条固定记录 `inband_signals`，类型 `CALL`。`evl_signal_thread` 调用 `post_bits(BIT(sig))`，`SIGDEBUG` 的原因码放在 `value` 里。投递时 `deliver_inband_signals` 对每个置位的信号调用一次 `send_inband_signal`（`now: thread.c:1422-1456`）。

#### P4 monitor（`orig: monitor.c:400-430`、`590-602`）

- **背景。** monitor 的 EVENT 类型（COUNT 计数器、MASK 位图）允许 Linux 线程用 `read()` 等待（doc 25）。oob 线程 post 之后，要叫醒睡在 `inband_wait_r` 上的 Linux 线程。
- **原版。**
  ```c
  smp_mb();
  if (waitqueue_active(&mon->inband_wait_r)) {
      if (running_inband())
          wake_up(&mon->inband_wait_r);
      else {
          evl_get_element(&mon->element);
          if (!irq_work_queue(&mon->inband_wake_r))
              evl_put_element(&mon->element);   /* 合并时把多取的引用还回去 */
      }
  }
  ```
  注意 `if (!irq_work_queue(...)) put`：**原作者明确处理了合并的情况**，说明合并是 irq_work 本来的设计用法。
- **现在。** `notify_inband_readers` 和 `notify_inband_writers` 调用 `evl_inband_notify_post(&mon->notify_r)`，类型 `WAKE_UP`，cookie 指向单独分配的 `ib->wait_r`（`now: monitor.c:27-50`）。不再取引用，销毁时先 drain。

#### P5 observable（`orig: observable.c:345-366`、`430-445`、`615-668`、`880-897`）

- **背景。** observable 是发布/订阅通道（doc 25）。有三种通知：
  - 有新消息时叫醒 inband 读者；
  - 有空位时叫醒 inband 写者；
  - oob 上摘掉的观察者要回到 inband 用 `kfree` 释放（flush）。
- **原版。** 三个 irq_work，写法和 P4 一样，都会"取引用，合并就还回去"。
- **现在。** 两条 `WAKE_UP` 记录，加一条 `CALL` 记录 `flush_call`（`inband_flush_call` → `do_flush_work`，`now: observable.c:452-455`）。flush 本身会处理队列里所有待释放的观察者，所以合并多次 flush 请求没有任何影响。

#### P6 stax（`orig: stax.c:285-350`）

- **背景。** stax 是一种"阶段互斥"锁：同一时刻只允许 oob 一方或 inband 一方进入临界区。inband 想进入时，如果 oob 正占着，就睡在 `inband_wait` 上。最后一个 oob 用户离开时，要叫醒它们。
- **原版。** `unlock_from_oob` 在并发计数归零时调用 `irq_work_queue(&stax->irq_work)`，func 执行 `wake_up_all`。
- **现在。** 记录类型 `WAKE_ALL`（`now: stax.c:340`）。等待者醒来后会在循环里重新检查 `inband_may_access(gate)`。

#### P7 `evl_call_inband`（`orig: work.c:37-108`）

- **背景。** 通用设施：oob 代码想让某个函数在 inband 的工作队列上下文里执行。
- **原版。** 两跳。`irq_work_queue` 到了 inband，`__evl_do_irq_work` 再执行 `queue_work(wq, &work->wq_work)`，工作线程最后调用 handler。如果 `evl_work` 带 element，就在 queue 时取引用，handler 结束后 put；**irq_work 合并或 `queue_work` 合并时都会把多取的引用还回去**（`work.c:45`、`101-104`）。
- **现在。** `struct evl_work` 里的 `irq_work` 换成一条 `CALL` 记录 `kick`，中继投递时调用 `__evl_do_inband_kick`，后者执行同样的 `queue_work`，引用规则也不变（`now: work.c:37-48`、`86-108`）。`evl_flush_work` 和 `evl_cancel_work` 原来调用 `irq_work_sync`，现在改为 `evl_inband_notify_drain`。

#### P8 crossing（`orig: include/evl/crossing.h`）

- **背景。** crossing 用来保证"关闭文件时没有 oob 操作还在进行"。oob 操作进出时 down、up 引用计数；inband 关闭文件时调用 `evl_pass_crossing` 等计数归零。
- **原版。** 最后一个 `up` 如果在 inband 且硬中断开着，就直接 `complete`，否则 `irq_work_queue`，func 再 `complete`。
- **现在。** 一律 `evl_inband_notify_post(&c->open)`，类型 `COMPLETE`；在 inband 且开中断时，post 内部会当场投递。`evl_pass_crossing` 在 `wait_for_completion` 之后调用 drain（`now: crossing.h:62-75`）。

#### P9 element 延迟销毁（`orig: factory.c:232-298`）

- **背景。** element 的最后一次 put 可能发生在 oob 上，或者发生在 inband 但硬关中断时。销毁必须在 inband 的任务上下文里进行，所以要把 element 挂到 flusher 线程的队列上。
- **原版。** 在 element 里 `init_irq_work` 一次，接着 `irq_work_queue`。func 拿 `flusher_wait.lock`，把 element 挂到 `flusher_queue` 上，如果队列原来是空的就 `wake_up_locked`。
- **现在。** 同样的流程，换成 element 里的一条 `put_call` 记录，标为 ONESHOT（`now: factory.c:255-296`）。flusher 可能在回调返回后立刻释放 element，ONESHOT 告诉中继：回调之后不再碰这条记录。
- **合并。** 每个 element 只有一次最后的 put，不会合并。

#### X-A、X-B xbuf（`orig: xbuf.c:336-350`、`478-494`）

- **背景。** xbuf 是 oob 与 inband 之间的字节管道（doc 22）。oob 写入数据后要叫醒 inband 读者；oob 读走数据腾出空间后，要叫醒 inband 写者。
- **原版。** `inbound_signal_input` 和 `outbound_signal_output` 每次都会 `irq_work_queue`，func 执行 `wake_up`。原版本来就在频繁合并：oob 写者连写 10 次，inband 读者往往只被叫醒一次，醒来后一次读走所有数据。
- **现在。** 每个环有一条 `WAKE_UP` 记录（`now: xbuf.c:243`、`318`）。

#### PX proxy（`orig: proxy.c:205-228`、`425-485`）

- **背景。** proxy 把 oob 的读写转给一个普通 Linux 文件，实际的 `vfs_write` 和 `vfs_read` 由 inband 的 relay 工作完成。
- **原版。** 通过 P7（`evl_call_inband_from(&ring->relay_work)`）。
- **现在。** 每个环有一条 `CALL` 记录，回调是 `relay_kick` → `queue_work`。post 前取 element 引用；如果 post 被合并，或 `queue_work` 被合并，就把引用还回去（`now: proxy.c:128-136`、`574-581`），和原版 P7 一样。

### 3.3 inband 上的"直接调用"

上表各项在 oob 上走 irq_work。如果调用者本来就在 inband，原版很多地方会**直接调用 Linux 函数**：

| 位置 | 原版直接做的事 |
|---|---|
| `monitor.c:404`、`596`、`850` | `wake_up(inband_wait_r/w)` |
| `observable.c:199`、`661`、`890`、`628` | `wake_up`、`do_flush_work` |
| `crossing.h:68` | `complete(&c->passed)` |
| `thread.c:1447` | `do_inband_signal` |
| `factory.c` | `__do_put_element` 或 `do_put_element_irq`（同步销毁，**没改**，因为这条路径整个在 inband 任务上下文里，属于 z 发起的操作） |
| `proxy.c:139`、`296` | `wake_up(inband_wait_w/r)`（这部分 inband 读写已在 doc 22 删除） |

现在，除 factory 的同步路径外，这些地方都统一调用 post。post 在 inband 且开中断时会**当场**跑中继的处理函数（§5.3），效果等于直接调用。

### 3.4 irq_work 以外的 Z → z 机制

| 机制 | 位置 | 做什么 | 现状 |
|---|---|---|---|
| `dovetail_send_mayday(p)` | `thread.c:1327`、`sched/core.c:110` | 在 Linux 任务的 thread_info 上置位，让它下次从用户态陷入时被强制切回 inband（用于 kick 或 demote 线程、看门狗） | 没改。它写的是 Dovetail 为 EVL 保留的标志，属于信任基（doc 23） |
| `dovetail_request_ucall(p)` | `thread.c:744`、`1180`、`1330`、`1800` 等 | 让线程返回用户态前回调 EVL | 同上 |
| `irq_post_inband(sirq)` | `drivers/evl/latmus.c:356` | 直接在 inband 中断日志登记一个合成中断 | 这正是中继用的底层原语 |
| 共享内存 | monitor 的 `sstate`、线程的 `u_window`、xbuf/proxy 的共享部分 | Z 写，z（用户态或 inband）自己读 | 不是"调用"，是"Z 写、z 读"，本来就符合规则 |

所以原版的出向调用通道只有 irq_work（含建在它上面的 `evl_call_inband`）。Dovetail 的 mayday 和 ucall 是另一类，现在归入信任基处理。

---

## 4. 合并会不会让正常 Linux 出错

### 4.1 先说清楚"通知"携带的是什么

上面所有通知都只表达一件事：**"状态变了，你去看一眼"**。真正的状态不在通知里，而在别处：

| 通知 | 状态在哪 | 接收方醒来后做什么 |
|---|---|---|
| P4 monitor | Z 里的计数 / 位图 | 重新 `trywait` |
| P5 observable 读 / 写 | Z 里的消息队列 | 重新 `pull` 或重新检查空位 |
| P6 stax | `stax->gate` | 循环检查 `inband_may_access` |
| X-A/X-B xbuf | 共享区的 `fill` | 循环检查条件 |
| P1 | 线程状态 | Linux 调度器调度它 |
| P5 flush、P7、PX | 一个队列，或者 work 自己 | handler 一次处理整个队列 |

Linux 的 `wait_event` 本来就是这样设计的：`wake_up` 不携带"几次"，等待者醒来必须重新检查条件，假唤醒和合并唤醒都是允许的。所以，只要满足"最后一次状态变化之后至少投递一次"，合并就不会丢任何东西。

### 4.2 中继保证"最后一次变化之后至少投递一次"

中继的两位协议和 irq_work 一一对应：

| irq_work | 中继（`now: kernel/evl/inband_notify.c`） |
|---|---|
| `irq_work_claim`：已 PENDING 就返回 false | `post`：`test_and_set_bit(PENDING)`，已置位就返回 false（`:24-46`） |
| 挂 Linux 的 `raised_list` | 挂 Z 的 `evl_notify_list`（per-CPU） |
| `irq_local_work_raise` → `irq_post_inband` + 必要时 sync | 完全相同的代码（`:37-44`） |
| `irq_work_single` ① 清 PENDING ② 执行 ③ 清 BUSY | 闸门 `take`：置 BUSY → 清 PENDING → 取 bits/value；处理函数执行动作；`done` 清 BUSY（`:81-115`） |

关键时序如下。设 Z 改了状态然后 post，z 正在投递：

```
情况 A：post 在"清 PENDING"之前
  Z: 改状态 → post → 看到 PENDING=1 → 返回 false（合并）
  z:                                  清 PENDING → 执行 wake_up
  → wake_up 在 Z 改状态之后执行，等待者能看到新状态。✓

情况 B：post 在"清 PENDING"之后
  z: 清 PENDING → 执行 wake_up（可能没看到新状态）
  Z:              改状态 → post → PENDING=0 → 重新排队 → 再投递一次
  → 新状态之后还有一次 wake_up。✓
```

这和 irq_work 的保证一模一样。`post_bits` 先 `atomic_or` 累积位再 post，闸门先清 PENDING 再 `xchg` 取走位，所以位也不会丢：要么被这一次取走，要么留给下一次投递。

### 4.3 跨记录的顺序

一个 CPU 上多条不同记录的投递顺序没有保证：llist 是后进先出。irq_work 的 `irq_work_run_list` 也一样。原版代码不依赖不同 irq_work 之间的顺序，现在也不依赖。

### 4.4 真正变了的三处

**① P3 信号：从"每个信号一个 irq_work"改为"按信号号累积"**

- 同一个信号在投递前到了两次，现在只会 `send_sig` 一次。
  - 对 `SIGTERM` 和 `SIGXCPU`（`SIGDEBUG`）这类标准信号，Linux 自己也是这样：目标任务还没处理第一个时，第二个会被 `legacy_queue()` 丢掉。中继投递之前目标任务不可能处理过第一个，所以两种做法最终送到用户态的信号次数相同。
  - 区别在于 `SIGDEBUG` 的原因码。原版里第二个 `send_sig_info` 被 Linux 丢弃，留下的是**第一个**原因；现在 `value` 会被覆盖，留下的是**最后一个**原因。这只影响诊断信息，不影响程序逻辑。如果需要和原版完全一致，可以改成"值已存在则不覆盖"。
- 不同信号的投递顺序变成按信号号从小到大。Linux 处理挂起的标准信号时本来也是按信号号挑，所以用户态看不到差别。
- 在 inband 上调用时，原版同步发送，现在由 post 当场投递；只有在"inband 虚拟关中断"的情况下，才会推迟到开中断时发送。

**② P4/P5 的引用：从"每次通知取一次引用"改为"销毁前 drain"**

- 原版通过引用保证 irq_work 执行时对象还活着。现在 `dispose` 里先调用 `evl_inband_notify_drain`，等记录不再处于 PENDING 或 BUSY，再释放对象（`now: monitor.c:1396-1403`、`observable.c:1148-1152`）。
- 对 Linux 一侧，效果相同：处理函数执行时，cookie 指向的等待队列一定还在。

**③ inband 上的直接调用改为 post**

- 当 inband 开着中断时，post 内部调用 `sync_current_irq_stage()`，在返回前就跑完了 `wake_up` 或 `complete`，和直接调用没有区别。
- 当 inband 处于虚拟关中断（stall）时，原版会直接调用，现在则推迟到开中断那一刻。被叫醒的是另一个任务，它本来就要等调度才能运行，所以推迟这一小段不会改变结果。原版的 irq_work 在同样的条件下也是这样推迟的（§2.2 Dovetail 的注释原话："we may only sync the in-band log when unstalled, with hard irqs on"）。

### 4.5 测试

doc 24 的回归测试：78 个测试通过 74 个，与未改动的 HEAD 相同，失败的 4 个也相同（`ptrace-sync` 缺 gdb，3 个 `sched-*` 是配置问题），dmesg 没有 WARNING。monitor、observable、poll、stax、xbuf、proxy 相关测试全部通过，其中包括 `monitor-count-inband`、`monitor-flags-inband`、`observable-inband`、`poll-observable-inband` 这些专门测 inband 等待者的用例。

### 4.6 "z 可以不投递"是什么意思

文档里多次写"z 可以延迟或跳过投递，只伤 z 的活性"。这是在威胁模型下说的：**如果 z 被攻破、恶意不处理中断**，Z 也不会因此出错，因为 Z 只是在自己的链表上挂了一个节点，没有任何东西在等它。

对**正常的** Linux，中继的中断处理函数和原来的 irq_work 中断处理函数一样，一定会执行。原版靠"Linux 一定执行 irq_work"，现在靠"Linux 一定执行这个合成中断"，两者的可靠性相同。

---

## 5. 现在的机制（`now: include/evl/inband_notify.h`、`kernel/evl/inband_notify.c`）

### 5.1 记录：放在 Z 对象里

```c
struct evl_inband_notify {
	struct llist_node node;   /* 挂在 Z 的 per-CPU 链表 */
	unsigned long flags;      /* PENDING / BUSY / ONESHOT */
	unsigned int type;        /* WAKE_UP / WAKE_ALL / WAKE_TASK / COMPLETE / CALL */
	unsigned int tag;
	void *cookie;             /* z 对象的地址，Z 只保存，从不解引用 */
	evl_notify_fn_t fn;       /* CALL 用 */
	atomic_long_t bits;       /* 累积位 */
	unsigned long value;      /* 最后一个值 */
};
```

z 拿到的是一份副本：

```c
struct evl_notify_event { type, tag, cookie, bits, value };
```

### 5.2 一次完整的通知（以 monitor 为例）

```
oob 线程 E: oob_write(monitor) 计数 +1
  │ notify_inband_readers(mon)
  │   smp_mb(); if (waitqueue_active(&mon->ib->wait_r))   ← 读 z 的值，只当提示
  │     evl_inband_notify_post(&mon->notify_r)
  │        test_and_set PENDING           ← 只写 Z 的记录
  │        llist_add(本 CPU 的 Z 链表)      ← 只写 Z 的链表
  │        irq_post_inband(evl_notify_sirq) ← Dovetail 原语（信任基）
  ▼ 返回，E 继续跑。整个过程 O(1)，不拿 z 的锁，不调 z 的代码

……inband 阶段恢复运行……

inband 合成中断: evl_notify_handler()
  while (evl_gate_notify_take(&t))   ← 闸门：从 Z 链表摘一条，复制出 t.ev
     wake_up(t.ev.cookie)            ← z 自己的动作，用 z 自己的锁
     evl_gate_notify_done(&t)        ← 清 BUSY
Linux 线程 L 醒来 → 重新 trywait → 拿到计数
```

### 5.3 inband 上 post

如果 post 本身在 inband 且开中断，post 内部会立即调用 `sync_current_irq_stage()`，处理函数在 post 返回前就执行完了。这与 Dovetail 的 `irq_local_work_raise` 完全相同。

### 5.4 生命周期

| 记录 | 释放前要做什么 |
|---|---|
| 普通记录 | 先保证不会再 post，再在 inband 任务上下文调用 `evl_inband_notify_drain(n)`，等 PENDING 和 BUSY 都清零 |
| ONESHOT 记录（P9） | 一生只 post 一次；接收方可以在回调里直接释放；中继在回调之后不再碰它；不能 drain |

### 5.5 和原来相比，Z 少依赖了什么

| | 原版 irq_work | 现在的中继 |
|---|---|---|
| 记录的字段谁写 | Linux 的 `irq_work_run` 写 flags 和节点 | 只有 Z 写；z 只能通过闸门让 BUSY 清零 |
| 链表 | Linux 的 `raised_list` | Z 的 `evl_notify_list` |
| Z 调用的代码 | `irq_work_queue`（Linux 代码） | `evl_inband_notify_post`（Z 代码）+ `irq_post_inband`（信任基） |
| Z 的堆 | P3 每个信号分配一次 | 不分配 |
| 引用 | 每次通知 get，在 z 回调里 put（z 不回调，Z 对象就永远不释放） | 不取引用；销毁时由 inband 自己 drain |

### 5.6 仍要注意的点

- `cookie` 和 `fn` 存在 Z 里，指向的却是 z 的东西。Z 不解引用它们，所以在逻辑划分下没有问题；做物理隔离时，要改成 z 侧的句柄（doc 24 N8）。
- `drain` 是 `schedule_timeout(1)` 轮询，只在销毁路径上用，性能无关紧要，但写法不够优雅。
- 网络栈 `net/output.c` 和测试驱动 `hectic.c` 还在用 irq_work（doc 24 N6）。

---

## 6. 问题 3：inband 等待路径的锁嵌套（doc 25 X8）

### 6.1 这条路径是什么

通知中继解决的是"Z 叫醒 z"。可是 z 要被叫醒，**它先得睡下去**。睡下去的代码是 EVL 写的，替普通 Linux 线程在 EVL 对象上等待：

- monitor：Linux 线程 `read(monitor_fd)`，计数为 0 时要睡；
- observable：Linux 线程 `read(observable_fd)`，没有消息时要睡；
- stax：inband 想进入临界区，但 oob 正占着时要睡。

这段代码在 inband 上、在普通 Linux 线程里运行，所以它是 **z 的执行流**。可它要做两件事：

1. 检查并修改 Z 的状态，比如试着扣计数（`trywait`），这需要 Z 的硬锁 `wchan.lock`；
2. 把自己挂到 Linux 等待队列上，这需要 Linux 的锁 `wq->lock`。

### 6.2 原版和现在的代码（monitor，`now: monitor.c:745-818`，原版相同）

```c
for (;;) {
	spin_lock_irqsave(&wq->lock, ib_flags);          /* ① 外层：Linux 等待队列的锁（z） */
	raw_spin_lock_irqsave(&wchan->lock, flags);      /* ② 内层：EVL 的硬锁（Z） */

	if (__trywait_locked)                            /* ③ 在 Z 锁下读改 Z 的计数 */
		goto __done;

	if (list_empty(&wq_entry.entry))
		__add_wait_queue(wq, &wq_entry);             /* ④ 仍持 Z 锁，写 Linux 的链表 */
	__set_current_state(TASK_INTERRUPTIBLE);

	raw_spin_unlock_irqrestore(&wchan->lock, flags);
	spin_unlock_irqrestore(&wq->lock, ib_flags);
	schedule();                                      /* ⑤ 睡 */
	...
}
```

observable 的 `pull_from_inband`（`orig: observable.c:776-825`）和 stax 的 `claim_stax_from_inband`（`orig: stax.c:156-215`）写法相同。stax 的注释直接写明了顺序：

> In-band lock first, oob next. First one disables irqs for the in-band stage only, second one disables hard irqs. Only this sequence is legit.

"Linux 锁套在 Z 硬锁外面"说的就是 ① 包着 ②。这个顺序本身没错：反过来先拿硬锁、再拿可能睡眠的 Linux 锁，在 Dovetail 下才是错的。

### 6.3 为什么原作者要这样嵌套

目的是防**丢唤醒**。如果不嵌套，可能出现：

```
z（等待方）                      Z（唤醒方，oob）
trywait 失败（计数=0）
                                计数 +1
                                waitqueue_active(wq)? 还没人 → 不通知
__add_wait_queue(wq)
schedule()   ← 睡死：计数已经是 1，但没人叫它
```

嵌套之后，③"检查失败"和④"挂上队列"在 Z 锁下一起完成。唤醒方改计数也要拿 Z 锁，所以它要么在 ③ 之前改（z 的 trywait 会成功），要么在 ④ 之后改（它的 `waitqueue_active` 会看到 z）。

### 6.4 这为什么是个问题

问题不在死锁，而在于这段代码**分不出属于哪一边**：

| 看它属于 z | 看它属于 Z |
|---|---|
| 在 Linux 线程里、inband 上运行，由 `read()` 触发，什么时候执行、执行多频繁都由 z 决定 | 持有 Z 的硬锁，读改 Z 的计数 / 消息队列 / gate |

按 doc 26 的规则，z 只能通过**门**影响 Z：门只接受数值，在 Z 自己的代码里拿锁、检查、修改。可这里是 z 的执行流自己拿了 Z 的硬锁，然后在锁内：

- **时序**：持锁多久，取决于 ④ `__add_wait_queue` 怎么执行，也就是写 z 的链表。z 的链表指针如果被改坏，写 `head->next->prev` 就会在持有 Z 硬锁时写到任意地址。逻辑划分下，z 本来就能写任何地方，这一条算不上新增风险。但一旦做物理隔离（doc 21 P8），z 根本拿不到 Z 的锁，这段代码就无法运行。
- **频率**：一个非实时进程不停地 `read()`，就能一直和 oob 线程抢 `wchan.lock`。xbuf 的"P 在等"一项（doc 22）也是这类问题。
- **边界**：同一段代码既改 Z 的状态，又改 z 的链表，没法把它整体划给任何一边。

这件事和通知机制无关：通知是**出向**（Z → z），这里是**入向**（z 进入 Z 去等待）。中继解决了"叫醒"那一半，"睡下"这一半还没拆。

### 6.5 怎么拆（建议，未实现）

不用嵌套也能防丢唤醒，Linux 的标准写法就是这样做的：

```
等待方（z，纯 Linux 代码）                 唤醒方（Z）
for (;;) {                                  在 Z 锁下改计数
    prepare_to_wait(wq, &e, TASK_INTERRUPTIBLE);  /* 挂队列 + 设状态，内含屏障 */
    if (gate_trywait(mon, &val))  break;    smp_mb();
    schedule();                             if (waitqueue_active(wq))
}                                               evl_inband_notify_post(...)
finish_wait(wq, &e);
```

- `prepare_to_wait` 先把自己挂上队列、设好状态，**再**检查条件。它内部的屏障和唤醒方的 `smp_mb` 配对，保证"等待方看到条件已满足"和"唤醒方看到有人在等"至少有一个成立，不会丢唤醒。
- `gate_trywait` 是 Z 提供的一个**门**：z 传入数值，Z 在自己的代码里拿锁、试扣、放锁，只返回结果。z 不再持有 Z 的锁去写 z 的链表。
- 唤醒方现在已经是这个形状（§5.2 的 `notify_inband_readers`），只需要改等待方。

拆完后，等待路径变成"纯 z 代码 + 一次门调用"，和 doc 22 里 xbuf 不可信端的做法一致。要改 monitor 的 COUNT 和 MASK、observable 的 `pull_from_inband`、stax 的 `claim_stax_from_inband` 四处，然后重跑 `monitor-count-inband`、`monitor-flags-inband`、`observable-inband`、`poll-observable-inband` 和 stax 测试。

---

## 7. 代码索引

| 内容 | 原版 | 现在 |
|---|---|---|
| irq_work 排队 / 执行 | `kernel/irq_work.c:57-130`、`201-233` | — |
| Dovetail 的 raise | `kernel/irq/pipeline.c:1715-1731` | 同 |
| 中继头文件 | — | `include/evl/inband_notify.h` |
| post / post_bits | — | `kernel/evl/inband_notify.c:24`、`52` |
| 闸门 take / done | — | `inband_notify.c:81`、`109` |
| 处理函数 | — | `inband_notify.c:117` |
| drain | — | `inband_notify.c:153` |
| P1 | `sched/core.c:1130`、`thread.c:810` | `sched/core.c:1131`、`thread.c:246` |
| P2 | `thread.c:455` | `thread.c:461-463`、`536` |
| P3 | `thread.c:1405-1460` | `thread.c:1402-1456` |
| P4 | `monitor.c:400-430`、`590-602` | `monitor.c:27-50`、`1348-1350`、`1401-1402` |
| P5 | `observable.c:350-366`、`438-445`、`625-665`、`885-895` | `observable.c:26-48`、`452-455`、`640`、`1114-1151` |
| P6 | `stax.c:286-291`、`345` | `stax.c:33-42`、`340` |
| P7 | `work.c:37-108` | `work.c:37-108`、`include/evl/work.h` |
| P8 | `crossing.h:36-77` | `crossing.h:37-75` |
| P9 | `factory.c:232-298` | `factory.c:255-296` |
| xbuf | `xbuf.c:336-350`、`478-494` | `xbuf.c:243`、`318`、`750`、`766` |
| proxy | `proxy.c:223`、`441`、`479` | `proxy.c:128-136`、`574-581`、`725`、`749` |
| inband 等待路径（§6） | `monitor.c:751-830`、`observable.c:776-825`、`stax.c:156-224` | `monitor.c:745-818`、`observable.c:770-825`、`stax.c:157` 起 |
