# EVL 如何调用 LKL

## 两种调用方式

### 直接调用（L0）

LKL 里写一个 `lkl_evl_xxx` 包装函数，加进 `LKL_ENTRY_POINTS` 使其保持全局；EVL 直接调用。不需要加锁，不需要 LKL 已经启动。

不能直接导出 `strcmp` 这类原名：会和宿主内核同名符号冲突。

### 进入 LKL 上下文（L1/L2）：`lkl_evl_call(fn, arg)`

照着 `lkl_syscall`（`arch/lkl/kernel/syscalls.c:106`）写，只是把"执行系统调用表里的函数"换成"执行任意函数"：

```
lkl_cpu_get()                 // 拿 LKL 的 CPU 锁
task = tls_get(task_key)      // 找到/创建调用线程对应的 LKL 宿主任务
switch_to_host_task(task)     // LKL 的 current 切到这个任务
ret = fn(arg)
lkl_cpu_put()                 // 收尾照抄 lkl_syscall
```

这样 LKL 内部即使睡眠也安全。

**必须实现 `tls_*` host_ops**，让每个调用线程有自己的 LKL 任务。曾经考虑过"所有调用方共用 `host0`"的简化，但锁类对象会出错：例如 mutex 的 lock 和 unlock 是两次独立调用，两次之间调用方在 LKL 外面，持有者记录的是 LKL 任务；共用 `host0` 时两个线程在 LKL 看来是同一个持有者，互斥失效。

`jmp_buf_set/longjmp` 也是必须的：`lkl_cpu_put` 会调 `thread_sched_jb()`，线程退出也用。内核里没有 libc，可用 GCC 的 `__builtin_setjmp/__builtin_longjmp`（LKL 调 longjmp 时传值正好是 1），或手写约 20 行 x86_64 汇编；该文件需设 `OBJECT_FILES_NON_STANDARD`。

## 开销

| 步骤 | 无竞争时 | 说明 |
|---|---|---|
| `lkl_cpu_get()`（`cpu.c:95`） | 一次 mutex 加解锁 + 比较 + 计数，百纳秒级 | |
| `switch_to_host_task()`（`threads.c:128`） | 当前任务已是自己就直接返回；否则跑一次 LKL `schedule()` | 调用方线程自己执行调度，再通过 jmp_buf 跳回，紧接着的 `sem_down` 拿到刚 `sem_up` 的信号量，不阻塞。**宿主层面不发生线程切换** |
| `lkl_cpu_put()` | 处理积压中断；有别的 LKL 任务可运行时让出 CPU | |

估计无竞争时单次进出在微秒级（需实测）。**有竞争时要等 LKL 当前持锁任务主动让出，没有上界。**

## 为什么开销多半不是问题

EVL 的 oob 路径本来就不调用会睡眠或依赖 Linux 状态的函数。例如 `poll.c`：oob 路径用 `evl_alloc`（EVL 堆）、`raw_spin_lock_irqsave`（硬锁）、`evl_get_file`；Linux 的 `kzalloc/kfree` 只在 inband 的 `poll_open`/`poll_release`（`poll.c:624/630/669`）。

所以需要进入 LKL 的调用基本都在慢路径（初始化、创建销毁 element、打开关闭文件、sysfs、mmap），这些操作本身就要几十到几百微秒。

## 单核的影响

LKL 相当于一把全局大锁，多个 CPU 同时调用只能排队。
- EVL 控制面并发度低，影响不大。
- 以后若把高频 inband 数据路径（如网络栈 inband 侧）搬进 LKL，会受限于单核吞吐。

## 实时性规则（硬规则）

> **oob 路径上只允许调用 L0 函数，永远不进入 LKL 上下文。**

oob 路径如果需要有状态的东西：要么由 EVL 自己管理（第二类），要么"inband 时由 LKL 预先分配好 + oob 时只用 L0 函数读写"。

## 降低开销的设计原则

1. **接口粗粒度**：一次进入 LKL 完成一整个操作（例：把 `factory.c:76-82` 的查找+置位循环做成一个 `lkl_evl_minor_alloc`）。
2. **计算和状态分开**：拆成"L0 纯计算 + 一次有状态提交"。
3. **能异步就异步**：释放、销毁这类不需要等结果的操作排队给 LKL 线程批量处理。
4. **预留跨 stage 开销**：host_ops 换成 EVL 原语后 LKL 线程就是 oob 线程，inband 调用方不能等 EVL 的锁，只能把请求投递给 LKL 线程再用 Linux completion 等结果，每次多两次跨 stage 唤醒。

建议实测：在 `lkl_evl_call` 前后用 `ktime_get_ns()` 计时，记录次数、平均、最大值。QEMU 纯模拟计时误差大，用 KVM 或真机。

## host_ops 的两阶段实现

1. **先用 Linux 原语**（代码放在 `kernel/evl/lkl_host.c`，结构上已是 EVL 提供）：kthread、semaphore、mutex（需支持递归）、hrtimer（回调在硬中断上下文，要唤醒一个 kthread 去调 LKL）、kmalloc/vmalloc（LKL 只要求虚拟地址连续）、`ktime_get_ns`。原因：inband 调用方不能等待 EVL 的 semaphore。
2. **再换成 EVL 原语**：`evl_run_kthread`、`evl_init_kmutex/evl_lock_kmutex`、`evl_down/evl_up`、`evl_init_timer/evl_start_timer`、`evl_alloc_chunk`（建议给 LKL 单独开一个 EVL heap）。LKL 线程用低优先级 EVL 线程，代码在 oob stage 执行。要确认 `print` 在 oob 上能否直接调 `printk`。

LKL 必须在 `evl_init()` 最开头启动（早于 `setup_init_state()`、`evl_early_init_factories()` 等）。

**L0 函数不需要上面任何一步**——不需要 host_ops，不需要 `lkl_init`/`lkl_start_kernel`。
