# commit 01：通知中继（Z → z 的通知只记录、不调用）

| 仓库 | commit | 标题 |
|---|---|---|
| linux-evl（`evl-lkl`） | `2495805ce5b4` | evl: relay in-band notifications through trusted records |

本文可以单独阅读。本文描述逻辑拆分，未实现物理隔离；记录地址不交给通知消费者，不代表所有 cookie 或回调都已摆脱可信对象指针。代码位置：`orig:` 指这个 commit 的父提交 `daed79c1cd9b`（原版 EVL 加上 LKL helper 迁移），`now:` 指这个 commit 之后的代码。Linux 本身的代码（`kernel/irq_work.c` 等）两边相同，不加前缀。

术语：

- **Z**：可信域，EVL 核心里实时任务依赖的那部分代码和数据。
- **z**：非可信域，Linux 内核（inband）。
- **oob / inband**：Dovetail 的两个执行阶段。oob 是实时阶段，inband 是普通 Linux。
- 规则只有一条：**Z 不依赖 z**。Z 不调用 z 的代码，不读写 z 的内存，不拿 z 的锁，也不等 z（详见 doc 26）。

文章按这个顺序展开：

1. Linux 自己为什么需要 irq_work 和 workqueue，它们各满足什么需求（§1）；
2. EVL 怎样复用这套机制（§2）；
3. `irq_work_queue()` 具体做了什么，为什么 Z 调用它不安全（§3）；
4. 我们改成了什么，为什么 workqueue 不用改（§4、§5）；
5. 用 proxy 做例子，对照改动前后（§6）。

---

## 1. Linux 里的问题：有些上下文什么都不能做

### 1.1 三种上下文

Linux 内核代码运行时所处的上下文，决定了它能调用哪些函数：

| 上下文 | 能拿普通自旋锁（`spin_lock`） | 能睡眠（`mutex`、`kmalloc(GFP_KERNEL)`、`kernel_write`…） | 例子 |
|---|---|---|---|
| **NMI** | 不能 | 不能 | perf 的采样中断、hardlockup 检测 |
| **关中断 / 硬中断** | 能（只能拿关中断那一类锁）；有些锁不能 | 不能 | 中断处理函数、持着 `rq->lock` 的调度器代码 |
| **进程上下文** | 能 | 能 | 系统调用、内核线程、kworker |

两条限制的根源不同：

- **不能拿锁**：NMI 可以打断任何代码，包括关了中断、正持有某把锁的代码。NMI 处理函数如果再去拿同一把锁，就会自己等自己，死锁。所以 NMI 里连 `wake_up()` 都不能调，因为 `wake_up()` 要拿等待队列的锁。调度器里持着 `rq->lock` 的代码也有类似限制：它不能调用可能再去拿 `rq->lock` 的函数。
- **不能睡眠**：中断上下文没有"自己的"任务可以挂起，睡眠就是 bug。

### 1.2 需求一：在"什么都不能做"的地方发起一件事 → irq_work

NMI 和关中断上下文里经常会发现"有件事要做"，但这件事本身要拿锁：

| 例子 | 发现者（受限上下文） | 要做的事 | 代码 |
|---|---|---|---|
| perf 采样 | NMI 里的 `__perf_event_overflow` 发现要通知用户态 | `perf_event_wakeup()`，要唤醒等在 fd 上的读者 | `kernel/events/core.c:9976` 排队，`6977` 执行 |
| printk | 任意上下文（可能是 NMI、可能持有调度器锁）打出一条日志 | `wake_up_interruptible(&log_wait)`，唤醒 `klogd`/`dmesg -w` | `kernel/printk/printk.c:4585` 排队，`4544` 执行 |
| schedutil 调频 | 调度器在持 `rq->lock` 时发现频率要调 | 叫醒调频内核线程（见 §1.3） | `kernel/sched/cpufreq_schedutil.c:136` 排队，`563` 执行 |

irq_work 就是为此设计的，文件头写得很清楚："Provides a framework for enqueueing and running callbacks from hardirq context. **The enqueueing is NMI-safe.**"（`kernel/irq_work.c:5-6`）。用法分两步：

1. **先登记**：在受限上下文里调用 `irq_work_queue(&work)`。它只做无锁的原子操作：把 `work` 挂到本 CPU 的链表上，再给自己发一个中断（x86 上是 APIC 自 IPI，向量 `IRQ_WORK_VECTOR`）。
2. **后执行**：当前代码退出受限状态、重新开中断时，自 IPI 到达，中断处理函数 `irq_work_run()` 取下链表，逐个调用 `work->func(work)`。

执行时仍然在**硬中断上下文**里，仍然不能睡眠。但这已经足够：只要相关锁遵守硬中断安全的加锁规则，`wake_up()`、`complete()`、`queue_work()` 这些"要拿锁但不睡眠"的函数都能安全调用。

所以 irq_work 满足的需求是：**从一个连锁都不能拿的上下文，跳到一个能拿锁、但还不能睡眠的上下文。**

### 1.3 需求二：要做的事需要睡眠 → workqueue

有些事连硬中断上下文都做不了，因为它需要睡眠：写文件、等 I/O、拿 mutex、`GFP_KERNEL` 分配。这时需要第二跳：在 irq_work 的回调里调用 `queue_work()`（或者 `kthread_queue_work()`），让内核线程（kworker）在**进程上下文**里执行。

schedutil 是典型的两跳（`kernel/sched/cpufreq_schedutil.c`）：

```
调度器（持 rq->lock，关中断）
  └ sugov_deferred_update() → irq_work_queue(&sg_policy->irq_work)        :136
硬中断：sugov_irq_work()
  └ kthread_queue_work(&sg_policy->worker, &sg_policy->work)              :569
内核线程（进程上下文）：sugov_work()
  └ mutex_lock(&sg_policy->work_lock); __cpufreq_driver_target(...)        可以睡眠
```

workqueue 满足的需求是：**从不能睡眠的上下文，跳到能睡眠的上下文。** 它自己的入口 `queue_work()` 要拿 workqueue 的锁，所以只能在"能拿锁"的上下文里调用，NMI 里不行。这就是为什么需要两跳，而不能一步到位。

| | irq_work | workqueue |
|---|---|---|
| 入口能在哪里调用 | 任何地方，包括 NMI | 能拿锁的地方（硬中断、进程上下文） |
| 回调在哪里执行 | 硬中断上下文 | 进程上下文（kworker） |
| 回调能拿锁 | 能 | 能 |
| 回调能睡眠 | 不能 | 能 |
| 解决的问题 | "此刻连锁都不能拿" | "此刻不能睡" |

---

## 2. EVL 怎样复用这套机制

### 2.1 对 Linux 来说，oob 就像 NMI

Dovetail 把中断分成两个阶段。Linux 的 `local_irq_disable()` 被改成只"虚拟关中断"（stall inband 阶段），硬件中断仍然开着。所以 oob 上的 EVL 代码可以在任何时刻打断 Linux，包括 Linux 关着（虚拟）中断、持有自旋锁的时候。

这和 NMI 的处境一模一样：**oob 上不能调用任何会拿 Linux 锁的函数**，否则可能和被打断的持锁者死锁。EVL 想让 Linux 做点什么（唤醒一个 Linux 任务、完成一个 completion、写一个文件），都必须先跳回 inband。

原版 EVL 的做法就是照搬 Linux 的两跳：

```
oob ──irq_work_queue──▶ inband 硬中断：wake_up / complete / queue_work ──▶ kworker：可睡眠的 handler
```

Dovetail 为此专门改过 irq_work 的触发方式（§3.1 第 4 步），让它从 oob 上调用也能工作。

### 2.2 原版全部的 Z → z 通知

`orig:` 中，EVL 核心在 oob 上（或者 inband 硬关中断时）通知 Linux 的地方共有 9 类，外加 xbuf、proxy 两个组件。全部用的是嵌在 EVL 对象里的 `struct irq_work`：

| 编号 | 场景 | 排队位置 | 回调做什么 |
|---|---|---|---|
| P1 | 线程从 oob 切回 inband | `sched/core.c:1130` | `wake_up_process(p)` |
| P2 | EVL 内核线程启动完成 | `thread.c:455-456` | `complete(&kthread->done)` |
| P3 | 给 EVL 线程发 Linux 信号 | `thread.c:1452-1459` | `send_sig_info`，然后释放引用和 `evl_free_rcu` |
| P4 | monitor 唤醒 inband 读者、写者 | `monitor.c:407`、`599` | `wake_up` + 释放引用 |
| P5 | observable 唤醒读者、写者，释放观察者 | `observable.c:664`、`893`、`631` | `wake_up` ×2；`do_flush_work`；都释放引用 |
| P6 | stax：oob 解锁后唤醒 inband 等待者 | `stax.c:345` | `wake_up_all` |
| P7 | `evl_call_inband`：在 inband 执行一个函数 | `work.c:101` | `queue_work`（第二跳） |
| P8 | crossing：最后一个 oob 用户离开 | `crossing.h:70` | `complete(&c->passed)` |
| P9 | element 的最后一次 put 发生在 oob 上 | `factory.c:286-287` | 挂到 flusher 队列并唤醒 flusher |
| — | xbuf：唤醒 inband 读者、写者 | `xbuf.c:348`、`492` | `wake_up` |
| — | proxy：叫醒转发者 | 经 P7 | `queue_work` → kworker 写目标文件 |

每一处的详细说明见 doc 27 §3。

### 2.3 用到 workqueue 第二跳的地方

P7 的 `evl_call_inband()` 就是"irq_work + workqueue"两跳的封装（`orig: work.c:86-108`、`include/evl/work.h`）：

```c
struct evl_work {
	struct irq_work irq_work;         /* 第一跳 */
	struct work_struct wq_work;       /* 第二跳 */
	struct workqueue_struct *wq;
	...
};

evl_call_inband_from(work, wq)        /* oob 上调用 */
  └ irq_work_queue(&work->irq_work)
硬中断：__evl_do_irq_work()
  └ queue_work(work->wq, &work->wq_work)
kworker：__evl_do_work() → work->handler(work)   /* 可睡眠 */
```

用户：proxy（写/读目标文件），网络栈（socket offload、skb 回收、时间戳），`lib/cache.c`（RCU 释放）。

---

## 3. `irq_work_queue()` 做了什么，Z 为什么不能调它

### 3.1 一次 `irq_work_queue()` 的全过程

数据结构（`include/linux/irq_work.h:17`）：

```c
struct irq_work {
	struct __call_single_node node;   /* llist 节点 + 原子 flags（PENDING/BUSY/LAZY/HARD_IRQ…） */
	void (*func)(struct irq_work *);  /* 回调 */
	struct rcuwait irqwait;           /* irq_work_sync() 用 */
};
```

排队（`kernel/irq_work.c:116`）：

1. **抢占记录**：`irq_work_claim()`（`:57`）对 `work->node.a_flags` 做 `atomic_fetch_or(IRQ_WORK_CLAIMED | CSD_TYPE_IRQ_WORK)`。原来已经 PENDING 就返回 false，表示"已经在排队，合并"。
2. **`preempt_disable()`**：改当前 CPU 的抢占计数。
3. **挂链表**：`__irq_work_queue_local()`（`:85`）读 flags 决定挂到哪条链表。LAZY 的、或者 PREEMPT_RT 下不是 HARD 的，挂 `lazy_list`；其余挂 `raised_list`。两条都是 **Linux 的 per-CPU 变量**（`:27-28`）。`llist_add` 把 `work->node.llist` 链进去。
4. **触发**：如果链表原来是空的，调 `irq_local_work_raise()`。原版 Linux 在这里发 APIC 自 IPI；Dovetail 把它改成了（`kernel/irq/pipeline.c:1715`）：
   ```c
   flags = hard_local_irq_save();
   irq_post_inband(inband_work_sirq);           /* 在 inband 中断日志里置一位 */
   if (running_inband() && !hard_irqs_disabled_flags(flags) && !irqs_disabled())
       sync_current_irq_stage();                /* 已在 inband 且开中断：当场执行 */
   hard_local_irq_restore(flags);
   ```
   中间还可能经过 tracepoint `trace_ipi_send_cpu`（`:106`）；LAZY 的 work 还要看 `tick_nohz_tick_stopped()`。
5. **`preempt_enable()`**。

执行（inband 恢复运行后）：

6. 合成中断 `inband_work_sirq` 的处理函数 `inband_work_interrupt()` 调用 `irq_work_run()`（`pipeline.c:1702`、`irq_work.c:259`）。
7. `irq_work_run_list()`（`:235`）`llist_del_all` 取下整条链表，对每个节点调用 `irq_work_single()`（`:201`）：
   - 清 PENDING（`atomic_set`，**写 work 的 flags**）；
   - `smp_mb()`；
   - **`work->func(work)`：把记录本身的地址交给回调**；
   - `atomic_cmpxchg` 清 BUSY（**再写一次 work 的 flags**）；
   - 某些配置下 `rcuwait_wake_up(&work->irqwait)`。

### 3.2 功能上它是 oob 安全的

第 1–5 步全是无锁原子操作，Dovetail 又专门把第 4 步改成了 `irq_post_inband`，所以从 oob 上调用不会死锁，也不会弄坏 Linux。原版 EVL 正是依赖这一点。

所以"不安全"说的不是死锁，而是**信任**：Z 一旦调用 `irq_work_queue()`，它的正确性就和 z 绑在了一起。

### 3.3 从信任的角度看，问题在哪里

| # | 问题 | 具体表现 | 违反的规则 |
|---|---|---|---|
| 1 | **Z 执行 z 的代码** | `irq_work_queue` 及其调用的 `irq_work_claim`、`__irq_work_queue_local`、`irq_local_work_raise`、tracepoint、`tick_nohz_tick_stopped` 都是 Linux 的代码。它们的行为取决于 Linux 的配置（PREEMPT_RT、LAZY、NOHZ、tracing），Z 无法对它们做出保证 | Z 不调用 z 的代码 |
| 2 | **Z 写 z 的内存** | `llist_add` 写的是 Linux 的 per-CPU `raised_list`；`preempt_disable` 写 Linux 的抢占计数 | Z 不写 z 的内存（"只写也不行"，doc 26） |
| 3 | **Z 的对象被链进 z 的数据结构** | 节点挂上 `raised_list` 之后，Linux 就持有了 Z 对象（monitor、thread、element……）内部的地址。第 7 步里 Linux 会**写这个 Z 对象**（清 PENDING、清 BUSY），还会**以它为参数调用回调** | z 不应持有、更不应写入 Z 的地址 |
| 4 | **Z 的记录格式由 z 定义** | `struct irq_work` 的布局、flags 的含义都是 Linux 的。Linux 把 PENDING 置住不清，Z 之后的所有 `irq_work_queue` 都会返回 false（"已经在排队"），通知就永远丢了 | Z 不依赖 z 的值 |
| 5 | **回调在 z 里拿着 Z 的指针** | 原版回调用 `container_of` 从 `struct irq_work *` 找回 Z 对象，再去读写它（`evl_put_element(&mon->element)`、读 `thread->altsched.task`、`evl_free_rcu(sigd)`）。也就是说，z 上下文里运行的代码直接操作 Z 的内存 | z 只能经门访问 Z |
| 6 | **Z 的资源被 z 扣住** | P3、P4、P5、P7 在排队前给 element 加引用，在回调里放引用。Linux 一直不执行回调，Z 对象就永远释放不了。P3 更是每发一个信号就从 EVL 堆里分配一块 `sig_irqwork_data`，Linux 不执行就耗尽 EVL 堆 | Z 不等 z（资源依赖） |
| 7 | **物理隔离时根本做不到** | 将来 Z、z 放进不同的保护域，Z 连 `raised_list` 的地址都访问不了 | — |

一句话：**irq_work 是 Linux 的机制，它的数据（链表、flags）和代码都在 z 里。** Z 调用它，等于把"通知 Linux"这件事的正确性交给了 Linux。

### 3.4 真正需要的只是哪一步

拆开看，Z 真正需要 Linux 提供的只有第 4 步里的那一行 `irq_post_inband(sirq)`：在 inband 中断日志里置一位，等 inband 恢复运行时触发一个合成中断。

`irq_post_inband` 是 Dovetail 的原语，属于信任基（doc 23 的 Y8）：它只写 Dovetail 自己的 per-CPU 中断日志，不拿锁，不读 Linux 的数据结构。其余步骤（记录、链表、标志、回调）都可以挪到 Z 里自己做。

---

## 4. 改成了什么：通知中继

### 4.1 一句话

**Z 只在自己的内存里登记"有事"，再让 Dovetail 在 inband 中断日志里置一位。inband 恢复运行时，z 侧的中断处理函数通过两个闸门从 Z 里取出这件事的副本，自己去做 `wake_up` 之类的动作。**

| 环节 | 归属 | Z 做的 | z 做的 |
|---|---|---|---|
| 登记 | Z | 写自己的记录，挂自己的链表 | — |
| 触发 | 信任基（Dovetail） | `irq_post_inband()` | — |
| 取出 | Z 的闸门 | 摘链表、改标志、复制值 | 调用闸门 |
| 执行 | z | — | `wake_up`、`complete`、`queue_work`…… |

### 4.2 结构

```
                 Z（可信）                                │            z（非可信，inband）
                                                         │
  EVL 对象里嵌一条记录 struct evl_inband_notify           │
     flags: PENDING / BUSY / ONESHOT                     │
     type, tag, cookie, fn, bits, value                  │
                                                         │
  post(n): test_and_set PENDING                          │
           llist_add(n, 本 CPU 的 evl_notify_list) ──────┼─── per-CPU 链表在 Z 里
           irq_post_inband(evl_notify_sirq)  ← 信任基    │
                                                         │
  闸门 take(&t): 摘一条 → 置 BUSY → 清 PENDING            │◀── evl_notify_handler()（合成中断处理函数）
                 → 复制 type/tag/cookie/fn/bits/value     │       while (take(&t))
                 → 本 CPU 记住"正在投递 n"                │           按 t.ev.type 做动作
  闸门 done():   清 BUSY（用的是 Z 自己记住的 n）         │◀──        done()
```

和 Linux irq_work 对照：

| | Linux irq_work | 中继 |
|---|---|---|
| 记录 | `struct irq_work`，布局由 Linux 定义，字段由 Linux 写 | `struct evl_inband_notify`，布局由 Z 定义，字段只由 Z 写 |
| 链表 | Linux 的 per-CPU `raised_list` | Z 的 per-CPU `evl_notify_list` |
| 排队 | `irq_work_queue()`（Linux 代码） | `evl_inband_notify_post()`（Z 代码） |
| 触发 | `irq_post_inband(inband_work_sirq)` | `irq_post_inband(evl_notify_sirq)`，同一个原语 |
| 执行 | `irq_work_run()` 遍历链表、改 flags，把**记录本身**交给回调 | z 的处理函数经闸门取**副本**，记录和链表从不交给 z |

可以说"把 `raised_list` 搬进了 Z"，但要补一句：**节点从不离开 Z**。z 拿到的是值的副本，连记录的地址都拿不到。

为什么不给每个对象分配一个合成中断，而要用链表复用一个？因为合成中断只能表达 1 bit："第 n 号有事"。EVL 的通知是"每个对象一条"，对象动态创建、数量没有上限，而合成中断域一共只有 1024 个号（`kernel/irq/pipeline.c:1878`）。分配和释放号还要走 Linux 的中断子系统。所以和 irq_work 一样，用一个中断号加一条链表来复用。

### 4.3 数据结构（`now: include/evl/inband_notify.h`）

```c
struct evl_inband_notify {          /* 嵌在 Z 对象里，只有 Z 的代码写 */
	struct llist_node node;
	unsigned long flags;        /* PENDING / BUSY / ONESHOT */
	unsigned int type;          /* 五种动作之一 */
	unsigned int tag;
	void *cookie;               /* z 对象的地址：Z 只保存，从不解引用 */
	evl_notify_fn_t fn;         /* CALL 用：z 的函数，Z 从不调用 */
	atomic_long_t bits;         /* post_bits 累积的位 */
	unsigned long value;        /* 最后一次设置的值 */
};

struct evl_notify_event {           /* z 收到的副本 */
	unsigned int type, tag;
	void *cookie;
	unsigned long bits, value;
};
```

| type | cookie 是什么 | z 做什么 | 使用者 |
|---|---|---|---|
| `WAKE_UP` | `wait_queue_head *` | `wake_up()` | monitor、observable（xbuf 在 commit 02） |
| `WAKE_ALL` | `wait_queue_head *` | `wake_up_all()` | stax |
| `WAKE_TASK` | `task_struct *` | `wake_up_process()` | 线程切回 inband |
| `COMPLETE` | `completion *` | `complete()` | kthread 启动、crossing |
| `CALL` | 任意 | `fn(&event)` | 信号、`evl_call_inband`、observable flush、element 延迟销毁（proxy 在 commit 02） |

| flag | 谁置 | 谁清 | 含义 |
|---|---|---|---|
| PENDING | post | 闸门 take | 已在链表上，还没被取走 |
| BUSY | 闸门 take | 闸门 done | 已取走，z 正在执行动作 |
| ONESHOT | 初始化时 | — | 接收方可能在回调里释放记录，take 之后 Z 不再碰它 |

前两个 flag 的协议照搬 irq_work 的 PENDING/BUSY，所以合并语义和 irq_work 完全相同。

### 4.4 post（`now: kernel/evl/inband_notify.c:24`）

```c
bool evl_inband_notify_post(struct evl_inband_notify *n)
{
	if (test_and_set_bit(EVL_NOTIFY_PENDING, &n->flags))
		return false;                          /* 已在排队：合并 */
	flags = hard_local_irq_save();             /* 不能中途换 CPU */
	llist_add(&n->node, this_cpu_ptr(&evl_notify_list));
	irq_post_inband(evl_notify_sirq);
	if (running_inband() &&
	    !hard_irqs_disabled_flags(flags) && !irqs_disabled())
		sync_current_irq_stage();              /* 已在 inband 且开中断：当场投递 */
	hard_local_irq_restore(flags);
	return true;
}
```

- 任何阶段、任何上下文都能调用，只做原子操作和 llist。
- 硬关中断包住 `llist_add` 和 `irq_post_inband`，保证记录挂到的就是收到合成中断的那个 CPU 的链表。
- 在 inband 上调用、而且中断开着时，当场投递，效果等于直接 `wake_up`。这和 Dovetail 的 `irq_local_work_raise()` 一样。
- `post_bits(n, bits)`（`:52`）先 `atomic_long_or` 累积位，再 post。信号用它：每个信号号一位。
- `set_value(n, v)` 只是 `WRITE_ONCE`，take 时读出最后一个值（SIGDEBUG 的原因码）。

### 4.5 两个闸门和处理函数（`now: inband_notify.c:62-170`）

处理函数 `evl_notify_handler()`（`:140`）是 z 的代码：它用 Linux 的 `__request_percpu_irq()` 注册（`:194`），由 Linux 的中断流程调用，做的全是 Linux 的事。它和 Z 的唯一接触是两个闸门：

```c
static irqreturn_t evl_notify_handler(int sirq, void *dev_id)
{
	struct evl_notify_take t;            /* { fn; ev } —— 只有值 */

	while (evl_gate_notify_take(&t)) {
		switch (t.ev.type) {
		case EVL_NOTIFY_WAKE_UP:   wake_up(t.ev.cookie); break;
		case EVL_NOTIFY_WAKE_ALL:  wake_up_all(t.ev.cookie); break;
		case EVL_NOTIFY_WAKE_TASK: wake_up_process(t.ev.cookie); break;
		case EVL_NOTIFY_COMPLETE:  complete(t.ev.cookie); break;
		case EVL_NOTIFY_CALL:      t.fn(&t.ev); break;
		}
		evl_gate_notify_done();
	}
	return IRQ_HANDLED;
}
```

**take 闸门**（`:93`）：
1. 硬关中断，从本 CPU 链表 `llist_del_first` 摘一条（此处以同 CPU 的 inband 处理函数不嵌套保证单消费者；硬关中断保护摘链操作）。
2. 复制 `type`、`tag`、`cookie`、`fn`。
3. 不是 ONESHOT 就置 BUSY，并把记录地址存进 Z 的 per-CPU 变量 `evl_notify_taken`。ONESHOT 记录存 NULL，因为接收方可能马上释放它。
4. 清 PENDING，`smp_mb__after_atomic()`。
5. `xchg` 取走累积的 `bits`，读 `value`。

先置 BUSY 再清 PENDING，drain 就不会看到两者都为 0 的空档。先清 PENDING 再取 bits，与 post 并发时，新 post 的位要么在这次被取走，要么重新排队，不会丢。

**done 闸门**（`:128`）不带参数：从 `evl_notify_taken` 取出记录，清 BUSY。z 交不进任何地址，所以伪造不了"要清哪条"。

**z 拿到的只有值。** 记录、链表、flags 的地址都不交给 z。cookie 和 fn 按动作解释。普通唤醒 cookie 指向 Linux 对象，但 evl_work 和 element 延迟销毁等 CALL 仍会携带可信对象地址；这类入口与对象拆分尚需后续收紧。

### 4.6 生命周期：drain 代替引用

| 记录 | 释放前要做什么 |
|---|---|
| 普通记录 | 先保证不会再 post，再在 inband 任务上下文里调用 `evl_inband_notify_drain(n)`（`:172`），轮询直到 PENDING 和 BUSY 都清零 |
| ONESHOT 记录 | 一生只 post 一次；接收方在回调里自己释放；不能 drain |

原版的做法是"每次排队取一次 element 引用，回调里放掉"，所以 z 不执行回调，Z 对象就永远释放不了（§3.3 第 6 条）。普通唤醒改由销毁路径（本来就在 inband 上）等待记录空闲；evl_work 的受保护 element 和 commit 02 的 proxy relay 仍保留引用。

这里要说清楚一点：drain 等的是 BUSY 被 z 的 done 闸门清掉，所以如果 z 拿走记录后一直不调 done，销毁路径会一直等。这仍然是一种资源依赖，只是从"Z 的 oob 路径"挪到了"inband 上的销毁路径"。Z 的实时路径从不等 z。

### 4.7 每个通知点改成了什么

| 编号 | 记录 | type | 位置（`now:`） |
|---|---|---|---|
| P1 | `thread->inband_wakeup` | `WAKE_TASK`，cookie 在映射时设为 `task_struct` | `sched/core.c:1131`，`thread.c:184-190`、`251` |
| P2 | `kthread->done_notify` | `COMPLETE` | `thread.c:466-468`、`541` |
| P3 | `thread->inband_signals` | `CALL` + `post_bits`，每个信号一位 | `thread.c:1422-1461` |
| P4 | `mon->notify_r/w` | `WAKE_UP`，cookie 是单独分配的 `mon->ib->wait_r/w` | `monitor.c:27-50` |
| P5 | `observable->notify_r/w`、`flush_call` | `WAKE_UP` ×2，`CALL` | `observable.c:26-48` |
| P6 | `stax->notify` | `WAKE_ALL` | `stax.c:33`、`340` |
| P7 | `evl_work.kick` | `CALL`，回调 `__evl_do_inband_kick` 做 `queue_work` | `work.c:37-50`、`include/evl/work.h` |
| P8 | `crossing->open` | `COMPLETE` | `crossing.h:37-75` |
| P9 | `element->put_call` | `CALL` + ONESHOT | `factory.c:250-296` |

xbuf 和 proxy 的通知随着它们的环拆分一起，在 commit 02 里改。

顺带做的改动：

- **monitor、observable 去掉了 `waitqueue_active()`**。原来 Z 先看一眼 inband 等待队列是否为空，再决定要不要 post。那是 Z 在读 z 的内存，现在改为无条件 post。没有等待者时多一次 O(1) 的 post，PENDING 合并保证 inband 多出的中断数有上界。等待队列头也从 Z 对象里挪了出去，单独分配（`struct evl_monitor_ib`、`struct evl_observable_ib`）。
- **信号不再按次分配 EVL 堆**。原版每个信号 `evl_alloc` 一块 `sig_irqwork_data`，现在累积在线程的一条固定记录里。同一信号在投递前多次发送会合并，这和 Linux 标准信号的语义一致；SIGDEBUG 的原因码保留最后一个。
- **factory 的三个原有 bug**（doc 20 的 B1–B3）：
  - B1：`flusher_kthread` 原来是一个全零的静态 `task_struct`，`kthread_stop` 停不到真正的线程，改为保存 `kthread_run` 的返回值；
  - B2：`getname_kernel` 的错误原来用 `== NULL` 判断，改为 `IS_ERR`；
  - B3：去掉多余的分号。

### 4.8 合并会不会出错

详细论证见 doc 27 §4，这里只列结论：

- 所有通知都只表达"状态变了，去看一眼"。状态本身在别处（Z 的计数、消息队列、共享区的 fill……），Linux 的 `wait_event` 本来就要求醒来后重新检查条件。
- 中继照搬 irq_work 的 PENDING/BUSY 协议：take 之前的多次 post 合并成一次投递，take 之后的 post 会重新排队。所以"最后一次变化之后至少投递一次"。
- 不同记录之间的投递顺序没有保证，这和 irq_work 一样，原版也不依赖它。
- 真正变了的语义只有三处：信号按信号号累积、drain 代替引用、inband 虚拟关中断时投递推迟到开中断。

---

## 5. 为什么 workqueue 不用管

改动前后对比（以 `evl_call_inband` 为例）：

```
原来： oob ──irq_work_queue──▶ inband 硬中断：__evl_do_irq_work ──queue_work──▶ kworker：handler
现在： oob ──evl_inband_notify_post──▶ inband 合成中断：__evl_do_inband_kick ──queue_work──▶ kworker：handler
                ▲ 改了这一跳                                        ▲ 这一跳没改
```

只换了第一跳，第二跳原封不动。原因是：**第二跳发生在 z 已经收到通知之后，完全是 z 内部的事。**

- `queue_work()` 由 z 的代码（中继处理函数里的回调 `__evl_do_inband_kick`）在 z 的上下文（inband 硬中断）里调用。Z 从不调用它。
- workqueue、kworker、`work_struct` 上的状态，都由 Linux 读写。Z 不读这些状态，也不等它们。
- z 不执行 work 的后果只落在 z 一侧：要么 inband 的事没做，要么一个选择了等待结果的 oob 调用者等不到（比如 proxy 的 oob 写者等缓冲区腾出空间）。Z 的内存和控制流都不受影响。这正是 doc 22 "非可信通道不保证正确性"的规则。

所以对 Z → z 的通知来说，要截断的只有"Z 怎样把消息交给 z"这一步。z 收到之后再怎样跳、跳几次，是 z 自己的事。

有一个遗留问题要注明：`struct evl_work` 里的 `wq_work`（一个 Linux 的 `work_struct`）仍然**嵌在** Z 对象里，Linux 会写它（`include/evl/work.h` 的注释写明了）。这不是"Z 调用 z"，而是"Linux 对象放在了 Z 的内存里"，属于嵌入 Linux 对象这一类问题（doc 24 的 N2），和 `stax->inband_wait`、`crossing->passed` 一起处理。proxy 在 commit 02 里已经把它的 `work_struct` 挪到了单独分配的不可信端（§6.3）。

---

## 6. 例子：proxy 的写路径

proxy 让 oob 线程写一个普通 Linux 文件（串口、管道、日志）：oob 线程把数据放进环形缓冲区，再由 inband 的转发者用 `kernel_write` 写到目标文件。它同时用到了两跳，是最完整的例子。

### 6.1 原来（`orig:`）

```
EVL 线程（oob）: oob_write(proxyfd, buf, n)
  proxy_oob_write → do_proxy_write                                    proxy.c:159
    拿 ring->lock（硬锁），预留空间，放锁
    raw_copy_from_user(ring->bufmem + wroff, ...)
    拿锁，nesting 降到 0：atomic_add_return(reserved, &ring->fillsz)
    缓冲区从空变非空：
      evl_call_inband_from(&ring->relay_work, ring->wq)               work.c:86
        evl_get_element(e)                     ← 给 proxy 加引用，等 z 来放
        irq_work_queue(&work->irq_work)        ← 在 oob 上执行 Linux 代码
          irq_work_claim：写 work->irq_work 的 flags
          llist_add 到 Linux 的 raised_list    ← Z 对象被链进 Linux 链表
          irq_local_work_raise → irq_post_inband(inband_work_sirq)
    放锁

……inband 恢复运行……

Linux：inband_work_interrupt → irq_work_run → irq_work_single
  清 PENDING（写 Z 对象）→ func(&work->irq_work)（把 Z 地址交给回调）
  __evl_do_irq_work                                                   work.c:37
    container_of 找回 evl_work（Z 对象）
    queue_work(work->wq, &work->wq_work)       ← wq_work 嵌在 Z 对象里
  清 BUSY（写 Z 对象）

kworker：__evl_do_work → relay_output_work → relay_output             proxy.c:145、75
    读 ring->fillsz、ring->rdoff                ← 直接读写 Z 的环状态
    kernel_write(filp, ...)
    atomic_sub_return(count, &ring->fillsz)    ← z 直接改 Z 依赖的计数
    evl_raise_flag(&ring->oob_wait)            ← z 直接唤醒 oob 写者
    wake_up(&ring->inband_wait_w)
    evl_put_element(e)                         ← z 放掉 Z 的引用
```

在通知这一段上，问题就是 §3.3 列的几条：Z 在 oob 上执行 Linux 代码、写 Linux 链表；Linux 持有并写入 Z 对象；回调拿着 Z 的指针。环状态本身也是两边共用的：`fillsz` 由 Z 加、z 减，z 把它改成任意值，就能让 Z 永远见满或者见空。这一点由 commit 02 的环拆分解决。

### 6.2 commit 01 之后

proxy 的代码这时还没动，但它调用的 `evl_call_inband_from` 已经换了第一跳：

```
EVL 线程（oob）: do_proxy_write → evl_call_inband_from(&ring->relay_work, ring->wq)
    evl_get_element(e)
    evl_inband_notify_post(&work->kick)        ← Z 代码，只写 Z 的记录和 Z 的链表
      irq_post_inband(evl_notify_sirq)         ← 信任基

z：evl_notify_handler
  take 闸门 → t = { CALL, cookie = work, fn = __evl_do_inband_kick }
  __evl_do_inband_kick(&t.ev)                                          work.c:38
    queue_work(work->wq, &work->wq_work)       ← 第二跳，不变
  done 闸门

kworker：relay_output（不变）
```

通知消费者不再取得中继记录地址或直接修改其 flags；但 CALL 的 cookie 此时仍是 evl_work 指针。环状态、嵌入的 wq_work 和回调访问可信对象的问题尚未全部拆开。

### 6.3 最终（commit 02 之后）

commit 02 把每个环拆成可信端、不可信端和共享部分（见 commit 02 的文档）。proxy 不再借用 `evl_work`，而是直接用一条中继记录，`work_struct` 挪进单独分配的不可信端：

```c
struct proxy_tend {                  /* 可信端：只有 Z 的代码读写 */
	struct evl_wait_queue wait;
	unsigned int fill, off, rsvd, req;
	int pending, waiters, error;
	bool eof;
	struct evl_inband_notify notify; /* 叫醒转发者，CALL，cookie = iend，fn = relay_kick */
};
struct proxy_iend {                  /* 不可信端：只有 inband 代码读写，单独分配 */
	struct mutex worker_lock;
	unsigned int off;
	struct work_struct work;         /* 第二跳的 work 在这里，不在 Z 里 */
	struct workqueue_struct *wq;
	wait_queue_head_t pollq;
	struct evl_proxy *proxy;
};
```

```
EVL 线程（oob）: proxy_oob_write                                        proxy.c:167
    拿可信锁 t->wait.wchan.lock；空间不够就睡在 Z 的等待队列上
    预留 t->off / t->rsvd，放锁，raw_copy_from_user，再拿锁
    pending 降到 0：t->fill += t->rsvd
    tend_publish：atomic_set_release(&sh->fill, t->fill)   ← 只发布，Z 自己从不回读
    tend_kick_relay                                                    proxy.c:131
      evl_get_element(&proxy->element)
      evl_inband_notify_post(&t->notify)       ← 只记录；已在排队就合并、放掉引用
    放锁，evl_schedule()

z：evl_notify_handler → take → relay_kick(&ev)                         proxy.c:574
    queue_work(ib->wq, &ib->work)              ← 第二跳；失败（已在队列里）就放掉引用
   → done

kworker：relay_work → relay_output                                     proxy.c:459
    count = min(读 sh->fill, bufsz)            ← z 只读发布的值
    kernel_write(filp, bufmem + ib->off, ...)  ← z 用自己的下标
    ib->off += count
    proxy_gate_consumed(proxy, count)          ← 门：Z 把 count 截断到 ≤ t->fill，
                                                  更新 t->fill，唤醒 Z 等待队列上的 oob 写者
    wake_up(&ib->pollq)
  evl_put_element
```

| | 原来 | 现在 |
|---|---|---|
| Z 怎样通知 z | 在 oob 上调用 `irq_work_queue`（Linux 代码），把 Z 对象链进 Linux 链表 | 写自己的记录，`irq_post_inband` |
| z 拿到什么 | Z 对象的地址（`struct irq_work *`），回调 `container_of` 回 Z 对象 | 值的副本：cookie（z 自己的 `proxy_iend`）和 fn |
| 谁写记录的 flags | Linux（`irq_work_single`） | 只有 Z（post 和两个闸门） |
| 第二跳的 `work_struct` | 嵌在 Z 的 `evl_work` 里 | 在 z 的 `proxy_iend` 里 |
| z 怎样报告进度 | 直接 `atomic_sub` Z 的 `fillsz`、直接 `evl_raise_flag` | 经门 `proxy_gate_consumed(n)`，Z 先截断再计入 |
| z 不干活的后果 | Z 对象的引用一直不放，对象释放不了；`fillsz` 可以被 z 改成任意值 | 等空间的 oob 写者可能一直等；relay 持有的引用也可能推迟销毁。drain / cancel 只在进入销毁路径后执行，不能替代 worker 放引用 |

---

## 7. Z/z 边界检查

| 检查项 | 结果 |
|---|---|
| Z 读 z 的内存 | 无。`waitqueue_active` 已经去掉 |
| Z 写 z 的内存 | post 不写。take 闸门把副本写进 z 提供的 `&t`（z 自己栈上的变量），这是闸门的输出参数 |
| Z 拿 z 的锁 | 无 |
| Z 调用 z 的代码 | 无，只调用信任基 `irq_post_inband` |
| Z 为通知分配内存 | 无，记录是对象里的固定字段 |
| Z 等 z | oob 路径不等。inband 上的销毁路径 drain 要等 z 调 done 闸门（§4.6） |
| z 读 Z 的内存 | 只经 take 闸门拿值副本；`evl_inband_notify_idle()` 读 flags（drain 用，只读） |
| z 写 Z 的内存 | 只经 done 闸门清 BUSY，地址由 Z 自己记 |
| z 能交给 Z 什么 | take 接收输出缓冲区地址；done 不接收记录地址。物理隔离时仍须约束输出缓冲区及调用状态 |
| z 不投递的后果 | 记录一直 PENDING，后续 post 全部合并。只有 z 的等待者睡死，Z 的状态不受影响 |

仍要注意的点：

- **cookie / fn 的含义取决于动作。** 通知核心只复制它们；CALL 中仍有携带可信对象地址、进入延迟清理的情况，须随对象模型和真实门接口继续拆分。
- **inband 等待路径的锁嵌套**（doc 27 §6，X8）：monitor、observable、stax 的 inband 等待代码仍然先拿 Linux 等待队列锁、再拿 Z 硬锁。这是 z → Z 方向的问题，留给后续处理。
- **drain 用 `schedule_timeout(1)` 轮询**，只在销毁路径上用。
- **网络栈 `net/` 和测试驱动 `hectic.c` 还在用 irq_work**，未在此次迁移中处理。当前 QEMU 配置关闭 EVL_NET，但开启 EVL_HECTIC，不能把驱动遗漏解释为未编译。

---

## 8. 验证

以下为 2026-11-13 的已有构建和运行记录；本次文档收尾未重跑。74/78 的口径为 77 项 libevl 测试加 1 项 pipe-stress。

- 这个 commit 单独编译通过（`kernel/evl/`、`drivers/evl/`，没有新警告）。
- 和后面三个 commit 一起在 QEMU 里跑 libevl 测试：74/78，失败的 4 个（`ptrace-sync`、3 个 `sched-*`）和改动前的基线相同，没有 WARN。

## 9. 相关文档

| 文档 | 内容 |
|---|---|
| doc 09、10 | 原版 proxy 写路径上的 irq_work，名词和数据结构 |
| doc 24 | 核心截断总表，中继第二版 |
| doc 27 | 原版每个通知点的逐个说明；合并为什么不出错的完整论证；inband 等待路径的锁嵌套 |
| doc 29 | 中继当前实现的详细说明（闸门只交值、`waitqueue_active` 的去掉） |
