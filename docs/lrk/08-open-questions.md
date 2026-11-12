# 待讨论问题（组会材料）

## 1. 时间（`ktime_get_mono_fast_ns`）能不能交给 LKL

EVL 的 `evl_ktime_monotonic()`（`include/evl/clock.h:83`）直接调 `ktime_get_mono_fast_ns()`，读 Linux 时间子系统的全局状态 `tk_fast_mono`（`kernel/time/timekeeping.c:105`）。是 oob 上最基础的依赖：`evl_mono_clock` 的读取函数（`clock.c:1076`）、调度器（`sched/quota.c`、`sched/tp.c`）、线程（`thread.c`）都靠它。

### 更新路径

只有负责计时的 CPU 在 tick 时更新，不是每个 CPU 每次中断都更新：

```
tick 中断（tick-common.c:100 / tick-sched.c:149）
  → update_wall_time()                    timekeeping.c:2258
  → timekeeping_advance(TK_ADV_TICK)
  → timekeeping_update()                  :655
  → update_fast_timekeeper(..., &tk_fast_mono)   :669
```

调时间（settimeofday/adjtimex）、休眠唤醒时也会更新。写者之间用 `timekeeper_lock` 互斥。

### 为什么读不用加锁：seqcount latch

不是原子读写（`tk_read_base` 有好几个字段）。存两份数据 + 一个序号：

```c
// 写者 update_fast_timekeeper (:322)
raw_write_seqcount_latch(&tkf->seq);   // seq 变奇数 → 读者改读 base[1]
memcpy(base, tkr, sizeof(*base));      // 改 base[0]
raw_write_seqcount_latch(&tkf->seq);   // seq 变偶数 → 读者改回读 base[0]
memcpy(base + 1, base, sizeof(*base)); // 改 base[1]

// 读者 __ktime_get_fast_ns (:340)
do {
	seq = raw_read_seqcount_latch(&tkf->seq);
	tkr = tkf->base + (seq & 0x01);    // 永远读当前没在被改的那一份
	now = ...;
} while (raw_read_seqcount_latch_retry(&tkf->seq, seq));
```

读者从来不需要等写者。

### 对 EVL 的意义

oob 可以在任意位置抢占 inband，包括 inband 正在 `timekeeping_update` 里改时间的时候（Dovetail 虚拟化了 inband 的关中断）。如果用普通 `ktime_get()`（普通 seqcount），读者看到奇数序号会一直重试，而写者就是被它抢占的同一 CPU 上的 inband 代码，永远等不到——死锁。latch 原本为 NMI 设计，正好解决这个问题，所以 EVL 必须用 `_fast_` 版本。

规律：**oob 想不加锁地读 Linux 的状态，Linux 那边必须用 NMI 安全的协议来写。**

### 迁移的问题

1. **循环依赖**：LKL 的时间来自 `host_ops->time()`，由 EVL 实现；EVL 又要读 LKL 的时间。可能的绕法：把 LKL 的时钟源直接接到 TSC。
2. **语义一致性**：用户态程序默认 EVL 单调时钟就是 Linux `CLOCK_MONOTONIC`，可以混用；复制两份会漂移。说明时间很可能是第三类对象。
3. **新类别**：读取不需要加锁。即使状态放进 LKL，oob 读者也可以不进入 LKL 上下文，按 latch 协议直接读——"有状态但可无锁读取"。

## 2. irq_work 交给 EVL+LKL 管理，还是复制一份

事实：
- EVL 通过 `evl_call_inband_from`（`work.c:101`）调 `irq_work_queue`，把工作从 oob 推迟到 inband。
- 实现：per-cpu 无锁链表 `llist_add`，再调 `irq_local_work_raise` 通知。原版 Linux 在这里给自己发 IPI（`arch/x86/kernel/irq_work.c:27`）；Dovetail 用强符号替换了它（`kernel/irq/pipeline.c:1715`），改成在 inband 中断日志里记一个合成中断 `inband_work_sirq`，回到 inband 回放日志时执行 `irq_work_run`。详见 [09](09-case-oob-irq-work.md)。
- Linux 自己大量使用 irq_work：printk、perf、RCU、调度器、BPF。

**方案 A（第二类）**：EVL 管理队列，Linux 通过 EVL 接口提交。

| 原则 | 评估 |
|---|---|
| 实时性 | 可以保证（本来就无锁）|
| EVL 尽量小 | EVL 接管一个 Linux 全局机制，Linux 所有 irq_work 都经过 EVL 代码，EVL 变大 |
| 改动尽量少 | 只改 `irq_work.c` 内部，使用者不用改，适中 |

问题：回调是 Linux 的函数，最终还要在 Linux 里执行；EVL 管队列没有减少依赖。

**方案 B（第一类）**：EVL 自己维护推迟工作链表（链表操作可用 LKL 的 L0 函数），需要通知 inband 时用 Dovetail 的 `irq_post_stage()`（`kernel/irq/pipeline.c:613`）投递合成中断，由其 inband 处理函数清空执行。EVL 完全不用 Linux 的 irq_work，Linux 那份不用动。

结论倾向：真正不能复制的只有通知机制本身（Dovetail 的中断注入），队列可以复制。

## 3. 配置常量和结构体布局

- LKL 和宿主配置不同，"同一个函数"行为可能不同：`HZ`（100 vs 1000）、`DCACHE_WORD_ACCESS`（`hashlen_string` 结果不同）、`nr_cpumask_bits`。
- 需要一种机制保证两边关键配置一致，或在胶水层显式转换？
- 能传给 LKL 的结构体必须两边布局相同，`sk_buff`、`task_struct` 等不行。网络栈迁移怎么处理？

## 4. kallsyms 重名符号

LKL 局部符号进入宿主 kallsyms，影响 kprobe/ftrace/BPF/livepatch。统一加前缀？还是从 kallsyms 里排除？

## 5. EVL 对 task_struct 等的依赖

`nm -u` 看不到的依赖（`current`、`thread_info` 里的 Dovetail 状态、`mm_struct` 里的 Dovetail 状态、per-cpu）占大头，基本都是 Dovetail 双内核接口的核心。这部分是第三类，LKL 能做的有限。如何界定"EVL 尽量小"的边界？

## 6. L1/L2 的调用开销与单核

- 无竞争微秒级（估计，待测），有竞争无上界。
- 单核 LKL 是全局大锁，控制面没问题，数据面会成瓶颈。
- host_ops 换成 EVL 原语后，inband 调用方要跨 stage 投递请求，每次多两次唤醒。
