# 03 oob → inband 中继另建一份（第三类：EVL 只是借用的服务）

## 1. 原本的机制

### 1.1 `evl_work` 是什么，谁在用

`evl_work` 是 EVL 的"从任意 stage 发起、最终在 inband 的 workqueue 里执行"的工作项。oob 代码不能直接调 `queue_work`（doc 13 §7.2 条件一），所以要先借 irq_work 跳到 inband，再在 inband 上 `queue_work`。

用到它的地方：

| 调用者 | 用途 |
|---|---|
| `proxy.c`：`do_proxy_write`、`proxy_oob_read`、`proxy_oob_poll` | 触发 `relay_output` / `relay_input`，在 inband 上读写目标文件 |
| `evl_call_inband_sync_from`（`work.c`） | 同步版本：oob 上等 `evl_flag`，inband 上 `flush_work` |
| `net/socket.c:286`、`net/timestamping.c:105`、`net/skb.c:92`（静态 `EVL_DEFINE_WORK`）、`lib/cache.c:311` | 网络栈和缓存的 inband 后续处理 |

### 1.2 原来的数据结构

`include/evl/work.h`（改动前）：

```c
struct evl_work {
	struct irq_work irq_work;          /* 第一跳：oob → inband */
	struct work_struct wq_work;        /* 第二跳：inband → kworker */
	struct workqueue_struct *wq;
	union {
		int (*handler)(void *arg);
		void (*handler_noreturn)(void *arg);
	};
	struct evl_element *element;       /* 异步调用期间持有的元素引用 */
};
```

### 1.3 原来的调用链

以 proxy 的输出方向为例：oob 线程写 proxy，ring 由空变非空，`do_proxy_write` 调用 `evl_call_inband_from(&ring->relay_work, ring->wq)`。

**oob 一侧**：

| 步骤 | 函数 | 位置 | 做什么 | 碰到的状态 |
|---|---|---|---|---|
| 1 | `evl_call_inband_from` | `kernel/evl/work.c:86`（改动前） | 记下 `wq`，`evl_get_element` 加元素引用 | EVL 私有 |
| 2 | `irq_work_queue` | `kernel/irq_work.c:116` | 入口 | — |
| 3 | `irq_work_claim` | `kernel/irq_work.c` | `atomic_fetch_or(IRQ_WORK_CLAIMED, &work->node.a_flags)`；已经 PENDING 就返回 false | `irq_work` 实例的 flags（内存在 EVL 的 `proxy` 里，协议是 Linux 的） |
| 4 | `preempt_disable` | `irq_work.c:123` | 防止迁移 | `pcpu_hot.preempt_count` |
| 5 | `__irq_work_queue_local` | `irq_work.c:85` | `llist_add` 挂进本 CPU 的 `raised_list`；如果链表原来是空的，调 `irq_local_work_raise` | **Linux 的 per-CPU `raised_list`** |
| 6 | `irq_local_work_raise` | `kernel/irq/pipeline.c:1715`（Dovetail 覆盖了 `irq_work.c:79` 的弱定义） | `hard_local_irq_save`，`irq_post_inband(inband_work_sirq)`；如果在 inband 且中断开着，立刻 `sync_current_irq_stage` | Dovetail 的 inband 中断日志 |
| 7 | `preempt_enable` | `irq_work.c:125` | 可能调 `preempt_schedule`，在 oob 上被 Dovetail 的守卫拦下（`sched/core.c:6987`） | `preempt_count` |

**inband 一侧**（回到 inband、中断日志被重放时）：

| 步骤 | 函数 | 位置 | 做什么 |
|---|---|---|---|
| 8 | `inband_work_interrupt` | `kernel/irq/pipeline.c:1702` | Dovetail 的 irq_work 合成中断处理函数，调 `irq_work_run` |
| 9 | `irq_work_run` → `irq_work_run_list` | `kernel/irq_work.c` | `llist_del_all(raised_list)`，逐个处理 |
| 10 | `irq_work_single` | `kernel/irq_work.c:201` | 清 PENDING，`smp_mb`，调回调，再用 `cmpxchg` 清 BUSY |
| 11 | `__evl_do_irq_work` | `kernel/evl/work.c:37`（改动前） | `queue_work(work->wq, &work->wq_work)`；如果已经在队列里，放掉第 1 步加的引用 |
| 12 | `__evl_do_work` | `kernel/evl/work.c:9` | kworker 里执行 `handler`（如 `relay_output_work`），最后放掉元素引用 |

**同步**：`evl_flush_work` / `evl_cancel_work`（`work.h`，改动前）先 `irq_work_sync(&work->irq_work)`。在 x86 上它就是 `while (irq_work_is_busy(work)) cpu_relax();`（`irq_work.c:304-305`），等第 10 步清掉 BUSY。

### 1.4 按 doc 13 的眼光看

原方案在 oob 上是安全的：第 3、5 步是无锁的原子操作和 `llist_add`，第 6 步关硬件中断，第 7 步有 Dovetail 守卫。

但 oob 每次交接都要碰两份与 Linux 共享的状态：

| 共享状态 | 还有谁在用 |
|---|---|
| per-CPU `raised_list` | 整个内核：perf、printk 的延迟输出、RCU、BPF、tracing，以及 EVL 自己其他直接调 `irq_work_queue` 的地方 |
| `irq_work` 实例的 flags（挂进 `raised_list` 之后） | Linux 的 `irq_work_run` 会独立地修改它 |

而 EVL 借用 irq_work，只是为了"回到 inband 时执行一个回调"。按 §11.5，这类状态应该**另建一份**：EVL 用自己的链表和自己的合成中断，Linux 的 `raised_list` 就不再和 EVL 共享。

## 2. 修改思路

### 2.1 总体

| 原来借用的 Linux 设施 | 改成 EVL 自己的 |
|---|---|
| `struct irq_work`（flags + 链表节点 + 回调指针） | `evl_work` 里的 `relay_flags` + `relay_node`，回调固定为 `do_relay_work` |
| Linux 的 per-CPU `raised_list` | EVL 的 per-CPU `evl_relay_list` |
| Dovetail 的 `inband_work_sirq` 及其处理函数 `inband_work_interrupt` → `irq_work_run` | EVL 自己申请的合成中断 `evl_relay_sirq`，处理函数 `relay_interrupt` |
| `irq_work_claim` / `irq_work_single` / `irq_work_sync` | `claim_relay` / `run_relay_work` / `evl_sync_relay`，协议一模一样 |

### 2.2 为什么能这样做

- **Dovetail 本来就提供合成中断**：`synthetic_irq_domain`（`kernel/irq/pipeline.c:37`）。Dovetail 初始化它时的注释写着 "The companion core may need a few of them as well"（`pipeline.c:1870` 附近），容量 1024。现成的先例有三个：
  - Dovetail 自己的 irq_work 中继（`pipeline.c:1881-1882`）；
  - `kernel/time/tick-proxy.c:332-337`；
  - EVL 的 `drivers/evl/latmus.c:410-436`。
- **合成中断的处理函数拿得到 per-CPU 参数**：`handle_synthetic_irq` 调处理函数时传的是 `raw_cpu_ptr(action->percpu_dev_id)`（`pipeline.c:199`）。把 `&evl_relay_list` 当作 `percpu_dev_id` 注册，处理函数直接拿到本 CPU 的链表。
- **协议照搬 irq_work**：PENDING / BUSY 两位的语义、`atomic_fetch_or` 认领、处理时先清 PENDING 再调回调、最后 `cmpxchg` 清 BUSY，全部和 `irq_work` 一致。这样 `evl_call_inband_from` 的返回值、重复提交、工作执行中再次提交、`evl_flush_work` / `evl_cancel_work` 的语义都不变，调用者不需要任何修改。

### 2.3 为什么不交给 LKL

链表操作 `llist_add` 需要真正的 `cmpxchg`，LKL 的原子操作是单核版本（doc 13 §9.1），不能用。这里直接用 Linux 的 `<linux/llist.h>`：`llist_add` 是内联的，它调用的 `llist_add_batch`（`lib/llist.c`）是一个只碰参数的无状态函数，对 oob 安全。将来如果要让 EVL 不依赖它，可以在 EVL 里放一份几行的副本。

## 3. 具体修改

### 3.1 `include/evl/work.h`

```c
#define EVL_WORK_PENDING	BIT(0)
#define EVL_WORK_BUSY		BIT(1)
#define EVL_WORK_CLAIMED	(EVL_WORK_PENDING|EVL_WORK_BUSY)

struct evl_work {
	struct llist_node relay_node; /* on per-CPU relay list */
	atomic_t relay_flags;	      /* EVL_WORK_PENDING|BUSY */
	struct work_struct wq_work;
	struct workqueue_struct *wq;
	...
};
```

- `EVL_DEFINE_WORK` / `EVL_DEFINE_SYNC_WORK` 的静态初始化里，`.irq_work = IRQ_WORK_INIT(__evl_do_irq_work)` 换成 `.relay_flags = ATOMIC_INIT(0)`。
- `evl_flush_work` / `evl_cancel_work` 里的 `irq_work_sync(&work->irq_work)` 换成 `evl_sync_relay(work)`。
- 删去 `__evl_do_irq_work` 的声明（没有外部调用者），新增 `evl_sync_relay`、`evl_init_relay`、`evl_cleanup_relay`。
- 保留 `#include <linux/irq_work.h>`，因为有些文件（`xbuf.c` 等）可能间接依赖它。

### 3.2 `kernel/evl/work.c`

新增的全局状态（第 20-22 行），都是 EVL 私有的：

```c
static DEFINE_PER_CPU(struct llist_head, evl_relay_list);

static unsigned int evl_relay_sirq;
```

**oob 或 inband：认领和入队**

```c
static bool claim_relay(struct evl_work *work) /* oob or in-band */
{
	int oflags;

	/* Same as irq_work_claim(). */
	oflags = atomic_fetch_or(EVL_WORK_CLAIMED, &work->relay_flags);

	return !(oflags & EVL_WORK_PENDING);
}

static void queue_relay(struct evl_work *work) /* oob or in-band */
{
	unsigned long flags;

	flags = hard_local_irq_save();

	if (llist_add(&work->relay_node, this_cpu_ptr(&evl_relay_list))) {
		irq_post_inband(evl_relay_sirq);
		if (running_inband() &&
			!hard_irqs_disabled_flags(flags) && !irqs_disabled())
			sync_current_irq_stage();
	}

	hard_local_irq_restore(flags);
}
```

`queue_relay` 对应原来第 4-7 步，结构照搬 `irq_local_work_raise`：

- 关硬件中断，保证取本 CPU 链表和发中断之间不会迁移，而且 `irq_post_inband` 本身要求关硬件中断（`pipeline.c:612` 的注释 "Must be called hard irqs off"）；
- 只有链表由空变非空时才发中断；
- 从 inband 调用、而且中断开着时，立刻重放，让处理函数马上执行。

原来的 `preempt_disable` / `preempt_enable` 被关硬件中断取代，于是第 7 步的隐式调度点也没有了。

`evl_call_inband_from`（第 179 行起）只把 `irq_work_queue(&work->irq_work)` 换成了"`claim_relay` 失败就放引用返回 false，否则 `queue_relay`"，其余不变。

**inband：合成中断处理**

```c
static irqreturn_t relay_interrupt(int sirq, void *dev_id) /* in-band */
{
	struct llist_head *list = dev_id;
	struct evl_work *work, *tmp;
	struct llist_node *node;

	node = llist_del_all(list);
	llist_for_each_entry_safe(work, tmp, node, relay_node)
		run_relay_work(work);

	return IRQ_HANDLED;
}

static void run_relay_work(struct evl_work *work) /* in-band */
{
	int flags;

	flags = atomic_read(&work->relay_flags);
	flags &= ~EVL_WORK_PENDING;
	atomic_set(&work->relay_flags, flags);

	/* Pairs with atomic_fetch_or() in claim_relay(). */
	smp_mb();

	do_relay_work(work);

	(void)atomic_cmpxchg(&work->relay_flags, flags,
			flags & ~EVL_WORK_BUSY);
}
```

`run_relay_work` 逐行对应 `irq_work_single`（`irq_work.c:201-228`）。`do_relay_work` 就是原来的 `__evl_do_irq_work`，内容不变，改为 `static`，参数直接是 `evl_work`。

**inband：同步**

```c
void evl_sync_relay(struct evl_work *work) /* in-band */
{
	lockdep_assert_irqs_enabled();
	might_sleep();

	/* Same as irq_work_sync() for archs with a work interrupt. */
	while (atomic_read(&work->relay_flags) & EVL_WORK_BUSY)
		cpu_relax();
}
```

**初始化**：`evl_init_work` / `evl_init_sync_work` 里的 `init_irq_work(...)` 换成 `atomic_set(&work->relay_flags, 0)`。

**中断的申请和释放**（第 226-251 行）：

```c
int __init evl_init_relay(void)
{
	int sirq, ret;

	sirq = irq_create_direct_mapping(synthetic_irq_domain);
	if (sirq == 0)
		return -EAGAIN;

	ret = __request_percpu_irq(sirq, relay_interrupt,
				IRQF_NO_THREAD, "EVL relay",
				&evl_relay_list);
	if (ret) {
		irq_dispose_mapping(sirq);
		return ret;
	}

	evl_relay_sirq = sirq;

	return 0;
}
```

写法和 `tick-proxy.c`、`latmus.c` 相同。`IRQF_NO_THREAD` 保证处理函数在中断上下文里执行，不被线程化。

### 3.3 `kernel/evl/init.c`

`init_core()` 在 `enable_oob_stage("EVL")` 之后第一个调用 `evl_init_relay()`，比任何可能发起 `evl_call_inband` 的子系统都早；失败路径上新增 `cleanup_relay` 标签，调用 `evl_cleanup_relay()`。

## 4. 前后对比

### 4.1 oob 一侧碰到的状态

| | 改动前 | 改动后 |
|---|---|---|
| 工作项的状态位 | `irq_work.node.a_flags`（Linux 协议） | `evl_work.relay_flags`（EVL 自己的字段，协议相同） |
| 挂到哪条链表 | Linux 的 per-CPU `raised_list`，全内核共用 | EVL 的 per-CPU `evl_relay_list`，只有 EVL 用 |
| 中断日志 | `inband_work_sirq` 那一位 | `evl_relay_sirq` 那一位 |
| `preempt_count` | `preempt_disable` / `preempt_enable` 各改一次 | 不改（改为关硬件中断） |

改动后，oob 一侧唯一碰到的 Linux / Dovetail 状态，是 Dovetail inband 中断日志里 `evl_relay_sirq` 的那一位。它属于 doc 13 §11.5 所说的共享区（Dovetail 中断日志）。

### 4.2 `work.o` 的外部符号

| 变化 | 符号 | 在哪个 stage 用 | 说明 |
|---|---|---|---|
| 删除 | `irq_work_queue` | oob / inband | 不再借用 Linux 的 irq_work |
| 新增 | `irq_post_stage`、`inband_stage` | oob / inband | `irq_post_inband` 展开后的 Dovetail 函数和它的 per-CPU 数据，D 域，原来在 `irq_work_queue` 内部同样会调用 |
| 新增 | `llist_add_batch` | oob / inband | Linux 的无状态函数，只碰 EVL 的链表，见 §2.3 |
| 新增 | `sync_current_irq_stage`、`inband_irqs_disabled` | 只在 inband 分支 | `running_inband()` 为真时才会执行 |
| 新增 | `irq_create_direct_mapping`、`irq_dispose_mapping`、`synthetic_irq_domain`、`__request_percpu_irq`、`free_percpu_irq` | 只在初始化 | inband，启动时调用一次 |

`proxy.o` 的外部符号不变：proxy 只通过 `evl_call_inband_from` 和 `evl_cancel_work` 间接使用中继。

## 5. 正确性

| 场景 | 论证 |
|---|---|
| oob 调 `evl_call_inband_from` | `claim_relay` 是一次原子操作；`queue_relay` 全程关硬件中断，`llist_add` 是 `cmpxchg` 循环。都属于 doc 13 §8.3 的非阻塞协议或关硬件中断，不等任何 inband 持有的东西 |
| oob 打断正在 `relay_interrupt` 里的 inband（同一个 CPU） | inband 用 `llist_del_all`（一次 `xchg`）整体摘下链表，之后只处理摘下来的那串。oob 新挂上来的节点进的是新的空链表，并重新发中断，下次重放时处理 |
| 工作执行中再次提交 | PENDING 已在 `run_relay_work` 开头清掉，`claim_relay` 能成功，再次入队；`run_relay_work` 末尾的 `cmpxchg` 发现 flags 变了，不会清掉新的 BUSY。和 irq_work 完全一样 |
| 重复提交（还没执行就再提交） | `claim_relay` 发现 PENDING，返回 false，`evl_call_inband_from` 放掉多加的元素引用并返回 false。和原来一样 |
| `evl_cancel_work`（`destroy_ring` 调用） | `evl_sync_relay` 等 BUSY 清掉，再 `cancel_work_sync`。和原来的 `irq_work_sync` + `cancel_work_sync` 等价 |
| 跨 CPU | 每个 CPU 的链表只由本 CPU 的合成中断处理函数摘取。一个工作项同一时刻只会挂在一条链表上（PENDING 保证），和 irq_work 一样 |
| 执行顺序 | `llist_del_all` 摘下的链表是后进先出，和 `irq_work_run_list` 的顺序相同。EVL 原本就不依赖多个工作项之间的顺序 |

一处行为差异：原来 EVL 的工作项和全内核的 irq_work 混在同一条链表上，由同一次中断处理；现在 EVL 的工作项由自己的中断处理，和 Linux 的 irq_work 互不排队。这只影响多个中继之间的相对时机，不影响任何一方的正确性。

## 6. 验证

**编译**（已做）：`build/linux-evl-qemu` 和 `build/linux-evl-full` 两种配置都通过，无警告。`linux-evl-full` 打开了 `CONFIG_EVL_NET`，覆盖了 `net/skb.c` 的静态 `EVL_DEFINE_WORK` 和 `net/socket.c`、`net/timestamping.c` 的调用。

**运行测试**（未做，计划）：

1. 启动后 `cat /proc/interrupts`，应该能看到名为 `EVL relay` 的 per-CPU 中断；
2. 跑 libevl 的 `tests/proxy-echo.c`、`proxy-pipe.c`、`proxy-eventfd.c`、`proxy-poll.c`：proxy 的输出和输入都依赖中继，功能正确即说明中继工作正常；运行前后 `EVL relay` 的计数应该增加；
3. 用 `evl_printf`（libevl 通过 stdout proxy 输出）在 oob 线程里大量打印，检验高频中继；
4. 在 proxy 还有未完成的中继时关闭它，覆盖 `destroy_ring` → `evl_cancel_work` → `evl_sync_relay`；
5. 如果配置了 `CONFIG_EVL_NET`，跑网络相关测试，覆盖 `net/` 下的调用者。

## 7. 局限

- **CPU 下线**：Linux 在 CPU 下线时会把该 CPU 上剩余的 irq_work 处理掉（`kernel/smp.c:101`，`smpcfd_dying_cpu` 里调 `irq_work_run`）。EVL 的中继没有做这一步，如果一个 CPU 下线时它的 `evl_relay_list` 上还有工作项，这些工作项不会被执行，元素引用也不会被放掉。EVL 一般不在运行中下线 oob CPU，但要彻底解决，需要加一个 CPU 热插拔回调，在下线时排空链表。
- **`evl_sync_relay` 忙等**：和原来的 `irq_work_sync` 一样，在 inband 任务上下文里忙等对方 CPU 处理完。语义没有变化，但如果对方 CPU 长时间处在 oob 上，忙等时间也会相应变长。
- **合成中断用了一个号**：从 `synthetic_irq_domain` 的 1024 个号里占用一个。

## 8. 后续

`kernel/evl` 里还有几处直接借用 Linux irq_work 的代码，可以用同样的方式迁到 EVL 的中继上。最简单的做法是把中继抽象成一个通用接口（"在 inband 上执行这个回调"），`evl_work` 只是它的一个用户：

| 位置 | 用途 |
|---|---|
| `factory.c:288`（`__evl_put_element`） | oob 上放掉最后一个元素引用时，交给 inband 释放 |
| `xbuf.c:348,492` | 唤醒 inband 读者 / 写者 |
| `monitor.c:407,599` | 唤醒 inband 等待者 |
| `observable.c:632,665,894` | 唤醒 inband 读者 / 写者，冲刷 |
| `sched/core.c:1130` | 线程切回 inband 时的后续处理 |
| Dovetail 的 `schedule_inband_fput`（`include/dovetail/fs.h`） | oob 上放掉最后一个文件引用 |

全部迁完之后，EVL 的 oob 代码就完全不再使用 Linux 的 `raised_list`。
