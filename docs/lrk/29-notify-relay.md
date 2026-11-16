# 29 通知中继详解（当前实现）

本文可以单独阅读，完整说明 EVL 现在怎样从可信域 Z 通知非可信域 z（Z → z，出向）。它取代 doc 27 §5；doc 27 的其余部分（原版 irq_work 的全部使用点、合并为什么不会弄坏 Linux、inband 等待路径的锁嵌套）仍然有效，本文只引用结论。

代码位置都指 `~/xenoami/linux-evl`。

---

## 0. 一句话

**Z 只在自己的内存里登记"有事"，再让 Dovetail 在 inband 中断日志里置一位；inband 恢复运行时，一个 z 侧的中断处理函数通过两个闸门从 Z 里取出这件事的副本，自己去做 `wake_up` 之类的动作。**

| 环节 | 归属 | Z 做的 | z 做的 |
|---|---|---|---|
| 登记 | Z | 写自己的记录、挂自己的链表 | — |
| 触发 | 信任基（Dovetail） | `irq_post_inband()` | — |
| 取出 | Z 的闸门 | 摘链表、改标志、复制值 | 调用闸门 |
| 执行 | z | — | `wake_up`、`complete`、`queue_work`…… |

Z 不读 z 的内存，不拿 z 的锁，不调 z 的代码，不分配内存，也不等 z。z 不投递，只会伤到 z 自己。

---

## 1. 为什么需要它

### 1.1 oob 上不能调用 Linux

`wake_up()` 要拿等待队列的 `spinlock_t`。Dovetail 下 `spin_lock_irqsave` 只"虚拟关中断"（stall inband 阶段），硬件中断还开着，oob 照样能抢占持锁者。oob 再去拿同一把锁就会死锁。所以 **oob 上不能调用任何会拿 Linux 普通锁的函数**，这是 Dovetail 的规则，原版 EVL 也必须遵守。

### 1.2 irq_work 是跳板，workqueue 是第二块跳板

Linux 内核里有三层上下文，能做的事一层比一层多：

| 上下文 | 能拿 Linux 普通自旋锁 | 能睡眠 | 典型例子 |
|---|---|---|---|
| NMI / EVL 的 oob | 不能（可能打断了持锁者） | 不能 | NMI 处理、EVL 实时线程 |
| （inband）硬中断上下文 | 能 | 不能 | irq_work 处理函数、本文的中继处理函数 |
| 进程上下文 | 能 | 能 | kworker 执行的 workqueue |

irq_work 的处理函数确实还是"关中断"运行的，但这不要紧：它解决的不是"关中断"，而是"此刻连锁都不能拿"。到了 inband 硬中断上下文，被打断的代码不可能持有"关中断才能拿"的锁，所以 `wake_up`、`complete`、`queue_work` 都能安全调用。要睡眠的事，再 `queue_work()` 跳到 kworker 上做。

EVL 原版就是这么跳的：

```
oob ──irq_work_queue──▶ inband 硬中断：wake_up / complete / queue_work ──▶ kworker：handler（可睡眠）
```

现在只换了第一跳：

```
oob ──evl_inband_notify_post──▶ inband 合成中断：evl_notify_handler（z）──▶ kworker（不变）
```

第二跳（workqueue）发生在 z 收到通知之后，是 z 自己的事，不需要再改。用到第二跳的是 `evl_call_inband()`（§6.3）。

---

## 2. 结构

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

和 Linux 的 irq_work 对照：

| | Linux irq_work | 中继 |
|---|---|---|
| 记录 | `struct irq_work`，嵌在 EVL 对象里，字段由 Linux 写 | `struct evl_inband_notify`，嵌在 EVL 对象里，字段只由 Z 写 |
| 链表 | Linux 的 per-CPU `raised_list` | Z 的 per-CPU `evl_notify_list` |
| 排队 | `irq_work_queue()`（Linux 代码） | `evl_inband_notify_post()`（Z 代码） |
| 触发 | `irq_local_work_raise()` → `irq_post_inband()` | 同样的 `irq_post_inband()` |
| 执行 | `irq_work_run()` 遍历链表、改 flags、调 `func(work)`，把**记录本身**交给回调 | z 的处理函数通过闸门取**副本**，记录和链表从不交给 z |

所以可以说"把 `raised_list` 搬进了 Z"，但要补一句：**节点从来不离开 Z**。z 拿到的是值的副本，连记录的地址都拿不到（§4.2）。

---

## 3. 数据结构（`include/evl/inband_notify.h`）

```c
struct evl_inband_notify {          /* 嵌在 Z 对象里，只有 Z 的代码写 */
	struct llist_node node;
	unsigned long flags;        /* PENDING / BUSY / ONESHOT */
	unsigned int type;          /* 五种动作之一，见下 */
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
| `WAKE_UP` | `wait_queue_head *` | `wake_up()` | monitor、observable、xbuf |
| `WAKE_ALL` | `wait_queue_head *` | `wake_up_all()` | stax |
| `WAKE_TASK` | `task_struct *` | `wake_up_process()` | 线程切回 inband |
| `COMPLETE` | `completion *` | `complete()` | kthread 启动、crossing |
| `CALL` | 任意 | `fn(&event)` | 信号、`evl_call_inband`、proxy、observable flush、element 延迟销毁 |

| flag | 谁置 | 谁清 | 含义 |
|---|---|---|---|
| PENDING | post | 闸门 take | 已在链表上，还没被取走 |
| BUSY | 闸门 take | 闸门 done | 已取走，z 正在执行动作 |
| ONESHOT | 初始化 | — | 接收方可能在回调里释放记录，take 之后 Z 不再碰它 |

---

## 4. 一次完整的通知（以 monitor 为例）

场景：Linux 线程 L 在 `read(monitor_fd)` 里等计数，EVL 线程 E 在 oob 上把计数加 1。

```
E（oob）: evl_post_sem → 在 Z 锁下计数 +1，放锁
  └ notify_inband_readers(mon)                         monitor.c:40
      evl_inband_notify_post(&mon->notify_r)           inband_notify.c:24
        test_and_set_bit(PENDING)  已置位 → 返回（合并）
        hard_local_irq_save
        llist_add(&n->node, 本 CPU 的 evl_notify_list)
        irq_post_inband(evl_notify_sirq)               在 inband 中断日志里置位
        hard_local_irq_restore
E 继续运行。O(1)，不碰 z 的任何东西。

……这个 CPU 上 inband 阶段恢复运行，Dovetail 同步中断日志……

evl_notify_handler()（z，inband 硬中断上下文）          inband_notify.c:140
  while (evl_gate_notify_take(&t))                     inband_notify.c:93
      wake_up(t.ev.cookie)          cookie = &mon->ib->wait_r，z 自己的等待队列
      evl_gate_notify_done()                           inband_notify.c:128
L 醒来 → 重新 trywait → 拿到计数
```

### 4.1 为什么去掉了 `waitqueue_active()`

原来 `notify_inband_readers` 先 `smp_mb(); if (waitqueue_active(&mon->ib->wait_r))` 再 post，目的是没有 inband 等待者时省掉一次 post。问题是：

- `mon->ib->wait_r` 是 z 的内存。Z 读它，是 Z 依赖 z 的值（入向）；
- `mon->ib` 是 Z 保存的、指向 z 对象的指针，读它就是 Z 去解引用 z 的地址；
- 将来做物理隔离时，Z 根本读不到 z 的内存。

读错了只会少一次或多一次唤醒，不会伤到 Z 的状态，所以 doc 27 时把它当"提示"放过了。现在改为**无条件 post**（`monitor.c:40-48`、`observable.c:39-47`），Z 完全不读 z。

**代价。** 没有 inband 等待者时也会 post：

| 情况 | oob 上多出的开销 | inband 上多出的开销 |
|---|---|---|
| 记录已 PENDING（上次还没投递） | 一次 `test_and_set_bit`，立即返回 | 无 |
| 记录空闲 | `test_and_set_bit` + 硬关中断 + `llist_add` + `irq_post_inband`，全部 O(1) | 一次合成中断，处理函数对空队列 `wake_up`（拿一次锁看一眼） |

PENDING 的合并让"每次 inband 恢复运行之间最多一次"成立，所以高频 post 时 inband 多出的中断数有上界。oob 一侧的最坏时间是常数，不影响实时性。

**丢唤醒。** 去掉检查后反而更简单：唤醒方每次改状态后都 post，处理函数在 post 之后执行 `wake_up`。等待方要么在 `wake_up` 拿等待队列锁之前已挂上队列（会被叫醒），要么之后才挂上，此时它重新检查条件，一定能看到新状态（等待队列锁的释放/获取把 Z 的状态写传递过去）。和 doc 27 §4.2 的合并论证组合起来，"最后一次状态变化之后至少有一次 `wake_up`"仍然成立。

### 4.2 闸门只交值，不交地址

现在两个闸门的形状（`inband_notify.c:62-137`）：

```c
struct evl_notify_take {                 /* z 拿到的东西 */
	evl_notify_fn_t fn;
	struct evl_notify_event ev;
};
static DEFINE_PER_CPU(struct evl_inband_notify *, evl_notify_taken);   /* Z 自己记 */

take(&t):  n = llist_del_first(本 CPU 链表)
           复制 type/tag/cookie/fn
           非 ONESHOT：置 BUSY，evl_notify_taken = n；ONESHOT：evl_notify_taken = NULL
           清 PENDING；smp_mb
           t->ev.bits = xchg(&n->bits, 0); t->ev.value = n->value
done():    n = evl_notify_taken；若非空：evl_notify_taken = NULL，清 BUSY
```

和 doc 27 时相比改了两处：

1. **`fn` 在 take 里复制。** 原来处理函数自己去读 `t.n->fn`，是 z 直接读 Z 的记录。
2. **done 不再接收记录地址。** 原来 z 把 take 给它的 `t.n` 交回 done，Z 就按这个地址清 BUSY；z 若交回一个伪造的地址，Z 会去写任意内存。现在 Z 用 per-CPU 变量 `evl_notify_taken` 自己记住"本 CPU 正在投递哪条"，done 不带参数。z 手里不再有任何 Z 的地址。

per-CPU 变量够用，因为：处理函数在 inband 硬中断上下文里运行，同一 CPU 上不会嵌套；oob 只 post，不调用闸门；处理函数在 `fn` 里也不会再进入处理函数（硬中断上下文里 post 不会当场同步，见 §5）。

ONESHOT 记录在 take 时就把 `evl_notify_taken` 置空，因为接收方可能在回调里释放它，done 不能再碰。

---

## 5. post 的细节（`inband_notify.c:24-60`）

```c
bool evl_inband_notify_post(struct evl_inband_notify *n)
{
	if (test_and_set_bit(EVL_NOTIFY_PENDING, &n->flags))
		return false;                                  /* 合并 */
	flags = hard_local_irq_save();                     /* 别在中途迁移 CPU */
	llist_add(&n->node, this_cpu_ptr(&evl_notify_list));
	irq_post_inband(evl_notify_sirq);
	if (running_inband() &&
	    !hard_irqs_disabled_flags(flags) && !irqs_disabled())
		sync_current_irq_stage();                      /* inband 且开中断：当场投递 */
	hard_local_irq_restore(flags);
	return true;
}
```

- **任何阶段、任何上下文都能调用。** oob、inband、硬关中断、NMI 式的上下文都可以，因为它只做原子操作和 llist。
- **硬关中断包住 `llist_add` 和 `irq_post_inband`**，保证节点挂到的就是收到合成中断的那个 CPU 的链表。
- **inband 上调用时当场投递。** 如果调用者本来就在 inband 且中断开着，`sync_current_irq_stage()` 会在 post 返回前跑完处理函数，效果等于直接 `wake_up`。这和 Dovetail 的 `irq_local_work_raise()` 一模一样。inband 虚拟关中断时，投递推迟到开中断那一刻。
- **`post_bits(n, bits)`** 先 `atomic_long_or` 累积位、`smp_mb__after_atomic`，再 post。take 先清 PENDING 再 `xchg` 取位，所以位不会丢：要么这次取走，要么留给下次。
- **`set_value(n, v)`** 只是 `WRITE_ONCE`，take 时读出最后一个值（信号的 SIGDEBUG 原因码用它）。

---

## 6. 处理函数和五种动作（`inband_notify.c:140-170`）

### 6.1 处理函数在 z

`evl_notify_handler()` 是 z 的代码：它用 Linux 的 `__request_percpu_irq()` 注册（`inband_notify.c:194`），由 Linux 的中断流程调用，做的全是 Linux 的事。它放在 Z 里就会变成"Z 执行 z 的代码"。它和 Z 的唯一接触是两个闸门。

### 6.2 WAKE_* / COMPLETE

直接对 `cookie` 调用相应的 Linux 函数。cookie 指向的等待队列、`task_struct`、completion 都是 z 的对象，生命周期由 drain 保证（§7）。

### 6.3 CALL，以及 workqueue 第二跳

`fn(&ev)` 是 z 的函数。几个典型回调：

| 记录 | fn | 做什么 |
|---|---|---|
| `thread->inband_signals` | `deliver_inband_signals` | 对 `ev.bits` 里每个信号 `send_sig_info`，SIGDEBUG 的原因码取 `ev.value` |
| `evl_work.kick` | `__evl_do_inband_kick` | `queue_work(work->wq, &work->wq_work)` → kworker 执行真正的 handler |
| proxy 的环 | `relay_kick` | `queue_work` → kworker 做 `vfs_write`/`vfs_read` |
| `observable->flush_call` | `inband_flush_call` | `kfree` 已摘下的观察者 |
| `element->put_call`（ONESHOT） | `put_element_call` | 挂到 flusher 队列并唤醒 flusher |

`evl_call_inband()`（`work.c:85-106`）就是"中继 + workqueue"两跳：Z 只 post `kick`；z 的 `__evl_do_inband_kick` 再 `queue_work`。用到它的地方：proxy、网络栈（socket offload、skb 回收、时间戳）、`lib/cache.c` 的 RCU 释放，凡是需要在进程上下文里做、可能睡眠的事都走这条路。

---

## 7. 生命周期

| 记录 | 释放前要做什么 |
|---|---|
| 普通记录 | 先保证不会再 post，再在 inband 任务上下文里 `evl_inband_notify_drain(n)`：轮询直到 PENDING 和 BUSY 都清零 |
| ONESHOT 记录 | 一生只 post 一次；接收方在回调里自己释放；不能 drain |

drain 代替了原版"每次通知取一次 element 引用、在回调里 put"的做法。原版的问题是：z 不回调，Z 对象就永远释放不了（Z 的资源被 z 扣住）。现在 Z 不取引用，销毁路径（本来就在 inband）自己等。

各对象的 drain 位置：monitor `monitor.c:1399-1400`、observable `observable.c:1148-1150`、stax `stax.c:42`、xbuf `xbuf.c:766`、proxy `proxy.c:749`、crossing `crossing.h:74`、kthread `thread.c:541`、线程 `thread.c:2716-2717`。

---

## 8. 合并为什么不会出错（摘要）

详细论证见 doc 27 §4，这里只列结论：

- 所有通知只表达"状态变了，去看一眼"，状态本身在别处（Z 的计数、消息队列、共享区的 fill……）。Linux 的 `wait_event` 本来就要求醒来后重查条件。
- 中继照搬 irq_work 的 PENDING/BUSY 协议：take 之前的多次 post 合并成一次投递；take 之后的 post 会重新排队。所以"最后一次变化之后至少投递一次"。
- 不同记录之间的投递顺序没有保证，和 irq_work 一样，原版也不依赖。
- 真正的语义变化只有三处：信号按信号号累积（SIGDEBUG 原因码留最后一个）、drain 代替引用、inband 虚拟关中断时推迟投递。

---

## 9. Z/z 边界检查

| 检查项 | 结果 |
|---|---|
| Z 读 z 的内存 | 无。`waitqueue_active` 已去掉（§4.1） |
| Z 写 z 的内存 | 无 |
| Z 拿 z 的锁 | 无 |
| Z 调 z 的代码 | 无。只调信任基 `irq_post_inband` |
| Z 等 z | 无。drain 在 inband 销毁路径上，等的是 z 自己的进度，Z 的 oob 路径从不等 |
| Z 为通知分配内存 | 无。记录是对象里的固定字段 |
| z 读 Z 的内存 | 只通过 take 闸门（值副本），`evl_inband_notify_idle()` 读 flags（drain 用，只读） |
| z 写 Z 的内存 | 只通过 done 闸门清 BUSY，地址由 Z 自己记 |
| z 能交给 Z 的东西 | 无参数。z 能做的只有"调或不调闸门" |
| z 不投递的后果 | 记录一直 PENDING，后续 post 全部合并；只有 z 的等待者睡死，Z 的状态不受影响 |

### 仍要注意的点

- **cookie、fn 是 z 的地址存在 Z 里。** Z 不解引用、不调用，逻辑划分下没问题；物理隔离时要换成 z 侧句柄（doc 24 N8）。
- **inband 等待路径（doc 27 §6，X8）。** monitor、observable、stax 的 inband 等待代码仍然先拿 Linux 等待队列锁、再拿 Z 硬锁。去掉 `waitqueue_active` 之后，doc 27 §6.5 的拆法更简单了：唤醒方已经无条件 post，等待方只要写成标准的 `prepare_to_wait` + 一次 `gate_trywait` 门调用即可，不再需要和唤醒方配对的 `smp_mb`。
- **drain 是 `schedule_timeout(1)` 轮询。** 只在销毁路径上用。
- **网络栈 `net/output.c` 和测试驱动 `hectic.c` 还在用 irq_work**（doc 24 N6），不在当前配置里。

---

## 10. 本次相对 doc 27 的改动

| 改动 | 位置 | 理由 |
|---|---|---|
| monitor、observable 去掉 `waitqueue_active()`，无条件 post | `monitor.c:32-48`、`observable.c:30-47` | Z 不读 z 的内存（§4.1） |
| take 闸门复制 `fn` | `inband_notify.c:68-72`、`111` | z 不直接读 Z 的记录 |
| done 闸门不带参数，Z 用 per-CPU `evl_notify_taken` 记住正在投递的记录 | `inband_notify.c:73-79`、`118`、`128-137` | z 不持有、不交回 Z 的地址 |

## 11. 测试

见 doc 30 §8：与线程控制权限的改动一起编译、一起跑。

## 12. 代码索引

| 内容 | 位置 |
|---|---|
| 头文件（类型、flags、记录、event、初始化） | `include/evl/inband_notify.h` |
| post / post_bits | `kernel/evl/inband_notify.c:24`、`52` |
| take 结构、`evl_notify_taken` | `inband_notify.c:68`、`79` |
| 闸门 take / done | `inband_notify.c:93`、`128` |
| 处理函数 | `inband_notify.c:140` |
| drain | `inband_notify.c:172` |
| 合成中断注册、注销 | `inband_notify.c:180-211` |
| monitor 唤醒方 | `monitor.c:40-48`，调用点 `428`、`594`、`841` |
| observable 唤醒方 | `observable.c:39-47`，调用点 `225`、`659`、`879` |
| `evl_call_inband` | `work.c:37-106` |
| 各对象记录初始化 | `thread.c:251-253`、`466`，`monitor.c:1346-1348`，`observable.c:1113-1117`，`stax.c:33`，`xbuf.c:750`，`proxy.c:725`，`factory.c:294-295` |
