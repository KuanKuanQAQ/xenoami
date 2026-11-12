# oob 与 Linux 共享状态：从 LKL 迁移到域划分的思路梳理

本文完整记录 2026-11-02 至 11-03 两天讨论形成的思路，按推导顺序展开：从动机出发，经过 LKL 的约束、划分方法的选择、"oob 任意打断 inband"这一根本原则，到函数和状态的判据、对 proxy.c / work.c 的逐一验证，最后给出共享状态的三类处理方法和下一步试验。

与其他文档的关系：
- [12](12-domain-partitioning.md) 是本文的前一版，侧重 SoK 框架和数据/函数分类表；文末有 KuanKuanQAQ 的总结和补充，本文以那份总结为骨架展开。
- [11](11-proxy-linux-deps.md) 是 proxy.c 依赖清单的原始数据。
- [02](02-object-classification.md) 的对象三分类、[03](03-function-levels.md) 的 L0/L1/L2 分级是本文判据的前身。

---

## 0. 一句话主线

> oob 可以在任意位置打断 inband，两者之间没有任何互斥。所以 oob 只能调用对 oob 安全的函数，只能访问协议对 oob 安全的共享字段；能交给 LKL 的只有无状态函数；与 Linux 共享的状态按 Linux 写的频率分三类处理，最后剩下一个必须信任的最小共享区。

> **2026-11-06 注**：本文默认"可信 = oob、不可信 = inband"，把"只在 inband 运行的 EVL 函数"一律视作 Linux 函数。这对时效性成立，对完整性不成立：写可信对象的 inband 代码（工厂、`.release` 等）也是可信代码。修正后的模型见 [14-trust-domain-vs-stage.md](14-trust-domain-vs-stage.md)，本文需要修正的地方已用"→ 见 doc 14 §12"标出，原文未改。

---

## 1. 动机

### 1.1 研究目标

把 EVL 核心对 Linux 的依赖迁到 LKL 上，由 EVL 为 LKL 提供 host_ops（[01](01-goals-and-principles.md)）。三条原则，按重要性排序：

1. 不破坏实时性；
2. EVL 尽量小；
3. 改动尽量少，可以随时撤掉。

### 1.2 走到哪一步了

第一阶段已经完成：把 EVL 调用的 37 处**无状态 helper**（rbtree、字符串、时间换算……）改成调用 LKL 里的同名函数（[05](05-dependency-inventory.md)、[06](06-build-pipeline.md)）。这些函数只碰参数和调用方的内存，LKL 甚至不需要启动。

第二阶段的障碍是**有状态依赖**，典型例子是 proxy 写路径上的 `irq_work_queue`（[09](09-case-oob-irq-work.md)）。把它换成 LKL 的同名函数，会出现正确性问题：

- LKL 的原子操作建立在 `irqs_enabled` 标志之上，只适用于单核，在多核和 oob 上都不成立；
- LKL 的 `arch_irq_work_raise` 是空的，链表要等 LKL 的 tick 才会被排空；
- LKL 的 `kernel_write` 拿到的是宿主的 `struct file`，对象根本不对。

### 1.3 由此引出的问题

在逐个分析 proxy.c 的依赖时（[11](11-proxy-linux-deps.md)），问题从"这个函数能不能换成 LKL"上升到了更一般的问题：

> **函数和数据应该按什么标准划分到 EVL 和 Linux 两个域？**

以及一个更远的设想：将来两个域装进不同的地址空间，跨域调用和跨域读写都要经过受校验的门，域内访问不校验，以保证低开销。

最初的定义是"按定义位置划分函数、按创建者划分对象"。用 proxy.c 一检验就不成立（[11](11-proxy-linux-deps.md) §1.2 列了 10 种反例）：

- Linux 头文件里的内联函数，机器码却在 EVL 里；
- `proxy` 由 Linux 的 `kzalloc` 创建，却只被 EVL 使用；
- 同一个 EVL 函数（`do_proxy_write`）在 inband 上会同步调用 `kernel_write`，在 oob 上却不能。

这说明"划分标准"背后其实混着三个问题：实时正确性、能不能迁移、怎么隔离。本文的思路就是把它们拆开，一个一个回答。

---

## 2. 先弄清 LKL 本身的约束

划分方法受 LKL 的性质限制，所以先确认几个事实。

### 2.1 LKL 没有硬件页表

- 我们用的配置是 NOMMU（`build/lkl-kabi/.config:159`，`# CONFIG_MMU is not set`）。LKL 启动时，`bootmem_init`（`lkl/arch/lkl/mm/bootmem.c:26-31`）通过 host_ops 的 `page_alloc` / `mem_alloc` 向宿主要一整块内存当作"物理内存"，之后的 buddy、slab、`kmalloc` 都在这块内存里切分，返回的就是宿主的虚拟地址。
- 可选的 `CONFIG_MMU` 只是软件模拟：有 `swapper_pg_dir`，但 CPU 不读它，真正的映射靠宿主的 `shmem_mmap`（`lkl/arch/lkl/mm/mmu_mem.c`），而且是为用户态 LKL 设计的。
- **意义**：LKL 不提供隔离能力，隔离只能由宿主一侧（EVL/Dovetail）来做。好在 LKL 的内存边界很清楚：blob 自己的段，加上启动时要来的那块内存池。

### 2.2 LKL 是单核的

- LKL 的"CPU"就是一把锁：`lkl_cpu_get` / `lkl_cpu_put`（`lkl/arch/lkl/kernel/cpu.c:95,113`）。上下文切换只是 `sem_up(next)` + `sem_down(prev)`（`threads.c:87-112`）。同一时刻只有一个宿主线程在执行 LKL 代码。
- 原因是可移植性：LKL 要在任何宿主上作为库运行，架构层不写汇编，而 SMP 需要真正的原子指令、per-CPU 基址、IPI 等架构支持。Kconfig 直接写了 `depends on !SMP`。
- **意义**：多个 CPU 上的 EVL 不能同时进入 LKL 的有状态部分；LKL 的原子操作、自旋锁在多核 oob 上都不成立。

### 2.3 一个进程一个实例

- 用户态里，每个进程调用一次 `lkl_init` + `lkl_start_kernel`，得到一个独立的内核实例：内存池、文件系统、调度器都是这个进程私有的。一个进程里只能有一个实例，因为所有全局变量都是单例。
- 放进宿主内核后，整个系统就相当于"一个进程"，只有一个实例。
- 只用无状态 helper 时，根本不需要启动 LKL 内核，现在 `lkl_ops` 为 NULL 也能工作，原因就在这里。

### 2.4 LKL 的调度器只管自己的锁

- LKL 调度器只决定"哪个宿主线程拿 CPU 锁"，线程什么时候真正上 CPU 由宿主调度器决定，是两级调度。
- LKL 内部也难以真正抢占：定时器中断要等持锁线程走到 `lkl_cpu_put` 或开中断时，才会被 `run_irqs` 处理。
- **意义**：实时线程一旦要等 LKL 内部的某个任务，延迟就取决于"宿主调度 + LKL 锁轮转"，没有上界。

### 2.5 小结

LKL 是一个单核、单例、没有隔离能力的库。由此直接得出：**oob 上只能调用 LKL 里的无状态函数**；有状态的部分最多只能在 inband 上串行使用。

---

## 3. 一个被否定的方案：每个 EVL 线程一个 LKL

想法：每个 EVL 线程有自己的 LKL 实例，这样每个实例只被一个线程使用，单核的前提就天然成立。

被否定的原因：

1. **EVL 的核心状态不属于任何一个线程**。proxy ring 被多个写者线程和一个 kworker 共享；运行队列、定时器、factory 名字表是全局的；调度器和定时器中断处理不代表任何线程运行。按线程切分，切不开。
2. **LKL 是单例**。blob 以 `-mcmodel=kernel` 非 PIC 编译，对 `.data` / `.bss` 用绝对地址访问，同一份代码无法指向不同的数据副本。一份 blob 的 `.data` + `.bss` 约 2.1MB，`.text` + `.rodata` 约 3.2MB（`size -A build/linux-evl-qemu/kernel/evl/lkl_blob.o`）。
3. **每个实例自带内部线程**（init、kthreadd、ksoftirqd、kworker），"一个实例只被一个线程使用"本身就不成立。
4. **创建慢、销毁难**：创建要跑一遍 `start_kernel`，毫秒级；LKL 基本不支持停机后干净回收。

这个方案其实属于 SoK 所说的"以数据为中心"划分，见下一节。

---

## 4. 划分方法：以代码为中心，加 stage 约束

### 4.1 SoK 的两种划分

参考 SoK: Software Compartmentalization（S&P'25，笔记见 [papers/](papers/SoK_Software_Compartmentalization_笔记.md)）。把程序画成矩阵，行是代码模块，列是被服务的对象：

```
              客户 A 的请求   客户 B 的请求   客户 C 的请求
解析模块        ■              ■              ■
图片解码库      ■              ■              ■
业务逻辑        ■              ■              ■
```

| 方法 | 怎么切 | 主体 | 防的是 | 例子 |
|---|---|---|---|---|
| 以代码为中心 | 横切：把某一行单独放进一个域 | 代码区域，编译时确定 | 某段代码出问题，殃及其他代码 | Firefox 的 RLBox 隔离第三方库 |
| 以数据为中心 | 竖切：每一列一个域，域里跑全套代码 | 执行实例（进程、线程），运行时动态创建 | 一条数据流攻破后碰到另一条数据流的数据 | Chrome 站点隔离、nginx 的 worker |
| 混合 | 执行实例被限定在某几行里 | 两者兼有 | — | — |

注意：SoK 的"以数据为中心"指**按执行实例划分**，不是"按数据归属划分"。

### 4.2 我们的矩阵

```
              EVL 线程 1   EVL 线程 2   kworker
EVL 代码        ■             ■            ■
Linux 代码      ■             ■            ■
```

我们要防的是 Linux 代码这一行用它的锁和调度破坏 EVL 代码这一行的实时性，这是横切的问题。"每个线程一个 LKL"是竖切，防的是线程 1 影响线程 2，这不是我们的威胁，而且这几列之间本来就要共享状态。

### 4.3 为什么还要加 stage 约束

Dovetail 的 stage 是一个时间维度：同一个 EVL 线程，这一刻在 oob，下一刻可能在 inband；同一段 EVL 代码，可能在两个 stage 上执行，而两个 stage 允许做的事完全不同。只按代码划分，表达不出"`do_proxy_write` 的 inband 分支可以调 `kernel_write`，oob 分支不可以"。

所以采用**混合划分**：

- 静态代码域三个：**E**（EVL + LKL blob）、**L**（宿主 Linux）、**D**（Dovetail，作为两者之间的仲裁层和门，属于 TCB）。
- 再加一条只在 oob 上生效的约束：**oob 上的执行只能停留在对 oob 安全的代码里**（§6）。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-1]

### 4.4 威胁模型

属于 SoK 的**保险箱**模型：保护 EVL 实时核心的可用性（最坏延迟有界）和完整性，不防 EVL 破坏 Linux，机密性不是目标。推论：**EVL 读 Linux 的内存本身不需要经过门**，需要限制的只是 Linux 写 EVL 依赖的状态。这一点在 §11 讨论共享区时很关键。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-2]

---

## 5. 哪些 EVL 函数需要处理

### 5.1 按 stage 给 EVL 函数分类

stage 是调用路径的性质，不完全是函数的性质。EVL 函数大致分五类：

| 类别 | 例子 | 源码中的线索 |
|---|---|---|
| 只在 oob | oob 的 fops（`proxy_oob_write`）、oob 中断处理（`evl_core_tick`、`oob_reschedule_interrupt`）、`__evl_schedule` | 7 处 `oob_context_only()` 断言（如 `sched/core.c:1082`、`mutex.c:338`） |
| 只在 inband | factory 的 build/dispose、各种 work、`relay_output` | 16 处 `inband_context_only()` 断言（如 `clock.c:191`、`memory.c:409`） |
| 两个 stage 都可能 | `evl_raise_flag`、`evl_get_element`、`do_proxy_write`、`evl_read_observable` | 25 处 `running_inband()` / `running_oob()` 分支 |
| 中途切换 stage | `evl_switch_inband`、`evl_switch_oob` | 前半段在一个 stage，后半段在另一个 |
| 通过函数指针被调用 | work 的 handler、`f_op` 的成员 | 取决于调用者 |

另外还有一种状态："在 inband 上，但硬件中断关着"。`__evl_put_element` 把它和 oob 同等对待（`factory.c:286`）。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-3]

### 5.2 只在 inband 上运行的 EVL 函数：不管

它们想调 Linux 函数就调，可以把它们视作 Linux 函数。例子：初始化函数、`relay_output`、`init_ring`、`proxy_factory_build`。这些函数只在 EVL 线程进入 inband 时，或者在 kworker、irq_work 处理函数里才会执行。

前提假设：

1. 启动过程中的 inband 是安全的；
2. 除了 `evl_switch_inband` / `evl_switch_oob` 这类显式切换函数，一个函数执行过程中不会换 stage。这一点依赖"oob 上不缺页"：如果 oob 函数中途缺页，线程会被降回 inband（EXDEMOTE），函数剩下的部分就会在 inband 上执行。而缺页本身属于应用没有锁定内存的问题（libevl 在 `lib/sys.c:169` 调用了 `mlockall`），不在本文讨论范围内。

唯一的限制：**调用 Linux 时不能持有 oob 也会用的锁**（如 `hard_spinlock_t`），否则 oob 等这把锁就等于在等 Linux。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-4]

### 5.3 `if (running_inband()) { ... }` 分支：抽成函数

把分支内部的代码抽成一个函数，它就变成了只在 inband 上运行的函数，归入 §5.2。三个现成的例子：

```c
/* do_proxy_write，proxy.c:218-223 */
if (running_inband()) {
	raw_spin_unlock_irqrestore(&ring->lock, flags);   // 先放 hard lock
	relay_output(proxy);                              // 内含 kernel_write
	return ret;
}
evl_call_inband_from(&ring->relay_work, ring->wq);    // oob 只走门

/* evl_call_inband_sync_from，work.c:118-127 */
if (unlikely(running_inband())) {
	evl_call_inband_from(work, wq);
	flush_work(&work->wq_work);                       // inband：Linux 的同步原语
	return sync_work->result;
}
evl_call_inband_from(work, wq);
return evl_wait_flag(&sync_work->done) ?: ...;        // oob：EVL 的等待对象

/* __evl_put_element，factory.c:286-298 */
if (unlikely(running_oob() || hard_irqs_disabled())) {
	init_irq_work(&e->irq_work, do_put_element_irq);
	irq_work_queue(&e->irq_work);                     // oob：交给 inband 释放
} else {
	... __do_put_element(e);                          // inband：直接释放
}
```

可见 EVL 的作者其实已经在非正式地遵守这条规则：oob 分支只走门，inband 分支随意。抽出来的函数要满足三个调用条件，可以在函数开头用断言检查：

1. 在 inband 上；
2. 硬件中断开着（Linux 代码默认这一点）；
3. 不持有 hard lock。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-5]

### 5.4 结果

去掉只在 inband 上运行的函数之后，剩下的就是**需要处理的 EVL 函数**：只在 oob 上运行的，以及可能在 oob 上运行的。下面所有讨论都针对它们对 Linux 的调用和访问。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-6]

### 5.5 静态分析的局限

用 objdump 从 23 个 oob 入口出发沿调用图搜索，得到 65 个函数、81 个外部符号，其中有明显的误报：

- **分支误报**：`evl_read_observable` 里的 `schedule`、`__wake_up` 在 `running_inband()` 分支里（`observable.c:817,846`），oob 实际上走不到；
- **中途切换误报**：`sched_setscheduler_nocheck`（`thread.c:1239`）在 `evl_switch_inband` 尾部被调用（`sched/core.c:1193`），这时已经切到 inband 了。

所以二进制级的调用图只能给出偏大的上界，要精确判断还得理解源码里的 stage 分支和切换点。§5.3 的"抽成函数"正好也让工具可以按函数来标注 stage。

---

## 6. 根本原则：oob 可以在任意位置打断 inband

### 6.1 为什么

Dovetail 把 Linux 的关中断改成了**虚拟的**：`local_irq_save`、`spin_lock_irqsave`、`raw_spin_lock_irqsave` 只设置一个 stall 位，CPU 的硬件中断仍然开着。只有 `hard_local_irq_save` 和 `hard_spinlock_t` 才真正屏蔽硬件中断。

因此，硬件中断到来、EVL 决定切换到某个 oob 线程时，inband 代码会**在任意位置**被冻结，包括它自以为"已经关了中断、拿着锁"的临界区内部。它要等 oob 一侧没事可做了才能继续。

- inband 代码之间的互斥仍然成立，因为 inband 的中断处理确实被推迟了；
- **inband 和 oob 之间不存在任何互斥**。

### 6.2 这只对"两边都碰"的状态构成威胁

如果一份 Linux 状态 oob 从不碰，inband 改到一半被打断也没关系：oob 看不到中间状态，inband 恢复后接着改完就行，代价只是延迟，而且只影响 inband。

所以真正的问题是两个：

1. oob 调用的函数，会不会碰到 inband 正在改的状态？（§7）
2. oob 访问的共享字段，协议能不能容忍被打断？（§8）

### 6.3 这条原则影响了什么

| 影响 | 例子 |
|---|---|
| oob 对 Linux 的"写"和"通知"都必须间接完成 | proxy 输出跳三次：ring → `irq_work_queue` → `queue_work` → `kernel_write`；`__evl_put_element` 在 oob 上挂 irq_work 再释放 |
| 锁的类型取决于 oob 会不会碰 | `ring->wroff` 两个 stage 都碰，用 hard lock；`ring->rdoff` 只有 `relay_output` 碰，可以用 Linux 的 mutex `worker_lock` |
| 等待对象要两套 | `oob_wait`（`evl_flag`）给 oob 等待者，`inband_wait_r/w`（Linux 等待队列）给 inband 等待者；oob 侧从不调 `wake_up` |
| Linux 代码需要 Dovetail 打补丁 | `fget` 不重试、`fput` 推迟、`printk` 只写缓冲区、irq_work 的抬起方式、`preempt_schedule` 的守卫（§7.3） |
| 对象的生命周期只能靠引用计数 | proxy 创建时 `fget` 钉住目标文件（`proxy.c:706`）；`evl_call_inband_from` 先 `evl_get_element`（`work.c:98-99`） |
| 迁移 LKL 的上限 | 有状态函数碰的必须是真正的 Linux 状态，不能换成 LKL 的（§9） |

---

## 7. 函数：什么叫对 oob 安全

### 7.1 oob 上其实一直在调用 Linux 函数

"oob 不能调用 Linux 函数"是错误的说法。proxy 的 oob 路径就调用了：

| 函数 | 调用点 | 是否安全 |
|---|---|---|
| `rep_movs_alternative` | `raw_copy_from_user` 内联展开后（`proxy.c:200`） | 安全 |
| `memset` | 拷贝出错时清零（`proxy.c:203`） | 安全 |
| `queued_spin_lock_slowpath` | `ring->lock` 竞争时 | 安全 |
| `irq_work_queue` | `evl_call_inband_from`（`work.c:101`） | 安全（经 Dovetail 改造） |

而 `queue_work`、`mutex_lock`、`kernel_write`、`kmalloc` 不能在 oob 上调用。区别在于下面三个条件。

### 7.2 三个条件

**条件一：不依赖被 oob 绕过的同步机制。**

反例 `queue_work`：`queue_work_on`（`kernel/workqueue.c:2415`）先 `local_irq_save`（虚拟），再 `raw_spin_lock(&pool->lock)`（`:2334`），最后可能 `wake_up_process`。同一个 CPU 上：

| 时刻 | inband | oob |
|---|---|---|
| t0 | 某个任务调用 `queue_work`，拿到 `pool->lock` | — |
| t1 | 被冻结在临界区里 | 硬件中断到达，EVL 切换到 oob 线程 T |
| t2 | 等 oob 让出 CPU 才能继续 | T 调用 `queue_work`，在 `pool->lock` 上自旋 |
| t3 | 永远等不到 | 永远等不到 |

结果是**死锁**。持锁者在别的 CPU 上时不会死锁，但等待时间没有上界。这首先是**正确性**问题，然后才是延迟问题。

正例 `irq_work_queue`（`kernel/irq_work.c:116`）：认领用 `atomic_fetch_or`，入队用 `llist_add`（`cmpxchg`），在任意位置被打断、被同 CPU 的其他上下文重入都正确。它本来就是为 NMI 设计的：**oob 打断 inband，本质上和 NMI 打断内核是同一种情况**，所以"对 NMI 安全"是"对 oob 安全"的一个好的近似。

**条件二：不睡眠，不进入 Linux 调度，包括退出时隐式的调度。**

- 反例 `mutex_lock`：拿不到锁就调 `schedule()`，而 Linux 调度器只管 inband。这也是 `proxy_write` 用 `wait_event_interruptible`（`proxy.c:507`）、`proxy_oob_write` 用 `evl_wait_flag`（`proxy.c:412`）的原因。
- 隐式调度：`preempt_enable()` 可能调用 `preempt_schedule()`；`local_irq_restore()` 会重放积压的 inband 中断。`irq_work_queue` 结尾就有 `preempt_enable()`（`irq_work.c:125`）。Dovetail 改了两处它才安全：`irq_local_work_raise`（`kernel/irq/pipeline.c:1715`）改为 `hard_local_irq_save` + `irq_post_inband`；`preempt_schedule()` 开头加了 `if (likely(!running_inband() || !preemptible())) return;`（`kernel/sched/core.c:6987`）。

**条件三：执行时间有上界。**

例如内存分配可能触发回收，所以 EVL 在 oob 上用自己的 `evl_heap`，不调 `kmalloc`。

### 7.3 Dovetail 的补丁标出了 Linux 的危险点

在 EVL 全部 oob 可达的 Linux 函数里，有一类是 Linux 原有的、经 Dovetail 打补丁才对 oob 安全的。补丁的位置正好说明 Linux 原来哪里不安全：

| 函数 | 原来的问题 | Dovetail 的改法 |
|---|---|---|
| `fget`（`fs/file.c:966-978`） | 文件正在关闭时会一直重试，等 inband 清掉 fd 表项；inband 若被冻结，oob 就一直转下去 | oob 上直接返回 NULL，不重试 |
| `fput`（`fs/file_table.c:535-541`） | 放最后一个引用时要调 `__fput`，可能睡眠 | oob 上改为 `schedule_inband_fput`，交给 inband |
| `printk`（`kernel/printk/printk.c:2373`，`internal.h:176`） | 输出控制台要拿驱动的锁 | oob 上只写无锁环形缓冲区，控制台输出推迟到 inband，和 NMI 的处理一样 |
| `irq_work_queue` | 结尾的 `preempt_enable` 和发 IPI | 见 §7.2 条件二 |

规律：**凡是"等别人完成"的地方都危险**（自旋锁、重试循环、等宽限期），改法要么是"不等，直接返回"，要么是"推迟到 inband"。

天生对 NMI 安全、不需要补丁的还有：

- `ktime_get_mono_fast_ns`：latch 双副本（`kernel/time/timekeeping.c:347`）；
- `___ratelimit`：用 `raw_spin_trylock_irqsave`（`lib/ratelimit.c:47`），拿不到锁就当作被限流，不自旋；
- tracepoint：`preempt_disable_notrace` 加 static call，`preempt_schedule_notrace` 同样有守卫（`sched/core.c:7033`）。

---

## 8. 状态：oob 能访问哪些 Linux 共享字段

### 8.1 术语：与 Linux 共享的状态

一开始说的是"Linux 全局状态"，不准确。`proxy.c:487-488` 读的 `proxy->filp->f_op` 不是全局变量，而是从参数一路指过去的对象。修正后的定义：

> 函数读写的某个对象实例，如果除了调用者之外，还有 Linux 代码在别处独立地读写它，它就是**与 Linux 共享的状态**（简称 Linux 共享对象：EVL 和 Linux 都会读写的对象）。怎么访问到它不影响判断。

| 访问路径 | 共享的例子 | 不共享的例子 |
|---|---|---|
| 全局变量或静态变量 | `tk_fast_mono`、`__cpu_online_mask` | — |
| per-CPU 变量 | `raised_list`、`qnodes`、`pcpu_hot` | — |
| 通过 `current` 间接访问 | fd 表（`current->files`）、IO 统计（`current->ioac`） | — |
| 参数指针 | `proxy->filp` 指向的 `struct file`、挂进 `raised_list` 后的 `irq_work` 实例 | `rb_insert_color` 改的 EVL 红黑树、`memset` 清的 `bufmem` |

两点补充：

1. **要看函数沿指针最终碰到的全部状态**。`ring->inband_wait_w` 在 EVL 内存里，但 `wake_up` 会顺着它碰到等待任务的 `task_struct` 和运行队列，这些都是共享的。
2. **嵌入的 Linux 结构，要看它被挂到哪里**。`relay_work.irq_work` 在 EVL 的 `proxy` 里，但一旦挂进 per-CPU `raised_list`，inband 的 `irq_work_run` 就会独立地修改它，从那一刻起就是共享的。

### 8.2 判断的单位是字段组，不是对象

同一个 `struct file` 里，`f_op` oob 可以随便读，`f_pos` 完全不能碰。所以：

| 概念 | 定义 | 决定什么 |
|---|---|---|
| 对象 | 一个结构体实例 | 生命周期和内存归属 |
| 字段组 | 一致性的最小单位：单个字段，或被一个不变式绑在一起的几个字段 | oob 能不能访问 |
| 协议 | 谁会写它、用什么机制保证读者看到一致的值 | 具体怎么访问 |

### 8.3 oob 能访问的共享字段：六种协议

| 协议 | 为什么在 oob 任意打断 inband 时仍然正确 | 例子 |
|---|---|---|
| **初始化后不变** | 没有写者 | `private_data`、`f_op`、`f_oob_state.data`、`this_cpu_off` |
| **非阻塞协议**：单字原子读写、无锁的读-改-写 | oob 从不需要等 inband 完成什么。`cmpxchg` 失败说明别人刚改成功，被冻结的 inband 不会再改，所以下次一定成功 | `f_flags`（单字）、`f_count`（`atomic_long_inc_not_zero`）、irq_work 的 `flags` 和 `raised_list` |
| **inband 改为关硬件中断，或用 hard lock** | inband 的临界区对 oob 是真正原子的。per-CPU 状态关硬件中断就够；跨 CPU 共享的状态要用 hard lock（§8.6） | `current_task`（inband 切换任务前 `hard_cond_local_irq_disable`，`sched/core.c:5164`）、EVL 等待通道 |
| **双副本（seqcount latch）** | 写者轮流更新两份，读者读那份没在改的；写者被冻结时读者读另一份即可，不需要重试 | `tk_fast_mono`（`timekeeping.c:105,347`），EVL 时钟经 `ktime_get_mono_fast_ns` 读取（`include/evl/clock.h:84`） |
| **per-CPU 嵌套（后进先出）** | 打断者总是先于被打断者完成 | `qnodes`（§8.5） |
| **交给 inband 去做** | oob 根本不访问 | `wake_up`、`queue_work`、`kernel_write`、`kfree` |

前五种是"怎么安全地访问"，最后一种是"不访问"。第二种的本质是：**oob 和 inband 之间只能用非阻塞协议**，单字读写只是其中最简单的情况。

### 8.4 oob 不能访问的

| 协议 | 为什么不行 | 例子 |
|---|---|---|
| Linux 锁、关抢占、虚拟关中断保护的多字段 | 可能读到改了一半的状态 | `f_pos`（配合 `f_pos_lock`）、等待队列链表、workqueue pool |
| 普通 seqcount | 读者遇到奇数序号会重试；写者在同 CPU 被冻结，序号永远是奇数 | timekeeping 主数据 |

**关于"单字"的准确含义**。x86_64 上单个对齐的 8 字节字段不会被硬件撕裂，真正的风险在于：

1. **字段比一个字长**：32 位平台上的 `loff_t`、`u64`（EVL 支持 32 位 ARM）；`NR_CPUS > 64` 时的 cpumask。
2. **跨字段不变式**（最常见）：时间 `{tv_sec, tv_nsec}` 跨秒更新时可能读到倒退近一秒的值；proxy 的 `avail = atomic_read(&ring->fillsz) - ring->reserved`（`proxy.c:335`）两个字段各自是单字，但差值才有意义，所以放在 `ring->lock` 里；链表的 `next/prev`；"指针 + 长度"。
3. **编译器**：64 位常量可能被拆成两条 32 位写；同一字段可能被读两次；位域写是对整个字的读-改-写。

所以"单字读写"要成立，必须同时满足：字段不超过一个机器字、自然对齐、读写都用 `READ_ONCE` / `WRITE_ONCE` 或原子操作，**并且不和其他字段构成不变式**。

### 8.5 per-CPU 嵌套是怎么回事

以 `queued_spin_lock_slowpath` 为例。锁被占用时，等待者要拿一个排队节点挂到队尾。节点不能临时分配，每个 CPU 预先准备 4 个（`qnodes[4]`，`kernel/locking/qspinlock.c:109`），再用一个计数器记录用掉了几个：

| 步骤 | 发生了什么 | `count` | 用的节点 |
|---|---|---|---|
| 1 | 普通任务等锁 A，`idx = count++` | 0 → 1 | `qnodes[0]` |
| 2 | 中断来了，中断处理也要等锁 B | 1 → 2 | `qnodes[1]` |
| 3 | 中断处理拿到锁 B、用完，`count--` | 2 → 1 | 归还 `qnodes[1]` |
| 4 | 回到普通任务，继续用 `qnodes[0]` | 1 | `qnodes[0]` |

正确性靠的是"打断者先于被打断者完成"，像叠盘子，后放的先拿。哪怕打断发生在 `count++` 的读和写之间也没关系，打断者用完会把 `count` 恢复原样。4 层对应普通任务、软中断、硬中断、NMI；用完时退化为反复 `trylock`（`qspinlock.c:413-415`）。

oob 打断 inband 正好满足"打断者先完成"：oob 在慢路径里关着硬件中断，拿到锁、放掉节点后才可能让出 CPU。所以 oob 只是多叠一层盘子。

### 8.6 hard lock 与 EVL 的锁

**hard lock** 是 Dovetail 的 `hard_spinlock_t`，加锁时做两件事，各管一个方向：

```
raw_spin_lock_irqsave(&ring->lock, flags)     // ring->lock 是 hard_spinlock_t
  → __hard_spin_lock_irqsave                  // spinlock_pipeline.h:144
      hard_local_irq_save()                   // ① 真正关掉本 CPU 的硬件中断
      do_raw_spin_lock()                      // ② 在锁变量上抢锁（原子 cmpxchg）
```

- **② 管其他 CPU**：硬件保证只有一个 CPU 抢得到。这是一条约定：所有碰这份数据的代码都先拿这把锁，有一方不遵守就失效。所以 oob 会碰的数据，inband 也必须拿同一把 hard lock。
- **① 管本 CPU**：持锁者不会被打断冻结。普通 `raw_spinlock_t` 只关虚拟中断，持锁的 inband 可能被冻结：本 CPU 的 oob 再来抢就死锁，别的 CPU 的 oob 来抢则等待没有上界。

两种锁在跨 CPU 互斥上一样，区别只在于持锁者能不能被冻结。代价是持锁期间屏蔽一切硬件中断，所以临界区必须非常短，`do_proxy_write` 拷贝前先放锁（`proxy.c:199-201`）就是这个原因。

**Dovetail 的三种自旋锁**：

| 类型 | 加锁时做什么 | 持锁者会被 oob 冻结吗 | 谁在用 |
|---|---|---|---|
| `raw_spinlock_t` / `spinlock_t` | 只设 stall 位，再抢锁 | 会 | Linux 绝大多数代码 |
| `hard_spinlock_t` | 关硬件中断，再抢锁 | 不会 | EVL 核心 |
| `hybrid_spinlock_t` | 关硬件中断 + 抢锁，inband 上还 `preempt_disable`（`kernel/locking/pipeline.c:31-43`） | 不会 | inband 和中断流水线都要碰的 Linux 对象，如 `irq_desc->lock` |

**EVL 在上面建的锁**：`evl_spinlock_t`（hard lock + 禁止 EVL 抢占，`include/evl/lock.h:80`）、`evl_mutex` / `evl_kmutex`（睡眠锁，带优先级继承）、`evl_ksem`、`evl_stax`（跨 stage 的睡眠锁，目前只有 `drivers/evl/hectic.c` 用）。

**睡眠锁也能互斥，但有三个限制**：

1. 睡眠锁内部状态要靠 hard lock 保护（`evl_mutex` 用 `wchan.lock`），自旋锁是最底层的互斥；
2. 中断上下文不能睡眠，所以运行队列、定时器、等待通道只能用 hard lock；
3. **跨 stage 时，oob 等待者可能在等 Linux**。EVL 把这类情况当作错误报告：持有 `evl_mutex` 时切回 inband 触发 `EVL_HMDIAG_LKDEPEND`（`mutex.c:261-265`，注释原话 "plain wrong since this would create a priority inversion"）；持有 mutex 时睡眠触发 `EVL_HMDIAG_LKSLEEP`（`wait.c:60-62`）；`evl_stax` 的 oob 线程因 inband 持有者而等待触发 `EVL_HMDIAG_STAGEX`。

选锁的原则只有一条：**oob 等待者永远不能依赖 Linux 调度器才能等到结果**。

| 锁 | 等待者最坏等什么 | oob 能用吗 |
|---|---|---|
| hard lock | 一段很短、不会被打断的临界区 | 能 |
| `evl_mutex`（持有者是 oob 线程） | 持有者在 EVL 调度下运行完，有优先级继承 | 能 |
| 持有者可能在 inband 的锁 | Linux 调度器 | 不能 |

### 8.7 生命周期

即使字段能读，oob 顺着指针访问对象时，还得保证对象没被释放。oob 不能靠 Linux 的锁或 RCU（RCU 宽限期由 inband 的静止状态决定，oob 读者可能不被看见），只能提前持有引用：proxy 创建时 `fget`，`dispose` 时才在 inband 上 `fput`；元素靠 `evl_get_element` 的引用计数。

### 8.8 判据

> oob 可以访问某个共享字段组，当且仅当：
> 1. 它的协议不依赖被 oob 绕过的互斥（初始化后不变 / 非阻塞 / hard lock 或关硬件中断 / latch / per-CPU 嵌套）；
> 2. 它所在对象的生命周期由引用计数保证，不依赖 Linux 的锁或 RCU。

这条判据与对象属于 EVL 还是 Linux 无关，只与字段的协议有关。

---

## 9. 迁移判定：无状态函数与有状态函数

### 9.1 定义

阻碍把 oob 上的 Linux 调用改成 LKL 调用的最主要原因，是被调用函数读写了与 Linux 共享的状态。改成 LKL 后，读写的就变成了 LKL 的状态，Linux 那份不会被更新，正确性就出问题。

- **无状态函数**：只做运算，只读写调用者私有的状态（参数、栈、调用者传进来的不共享内存），**并且不依赖 LKL 的单核 arch 原语**（原子操作、关中断、`current`、`preempt_count`）。可以直接改调 LKL。
- **有状态函数**：会读写与 Linux 共享的状态。

第二个条件不能省。反例是 `llist_add_batch`：链表头如果是 EVL 的，它就不碰任何 Linux 状态，但它需要真正的 `cmpxchg` 才正确，交给 LKL 会在多核上出错（[09](09-case-oob-irq-work.md) 第 168 行）。

### 9.2 两个问题要分开问

| | 无状态 | 有 Linux 共享状态 |
|---|---|---|
| **对 oob 安全** | `memset`、`rep_movs_alternative`、`rb_insert_color`：改调 LKL 或编进 EVL，之后不算跨域 | `irq_work_queue`、`fget`、`queued_spin_lock_slowpath`：留在 Linux，**这就是门的集合**，要尽量小并经过审核 |
| **对 oob 不安全** | 少见 | `queue_work`、`mutex_lock`、`kernel_write`、`kmalloc`：只能在 inband 调用；出现在 oob 上就是 bug，换成 LKL 也解决不了 |

- "能不能在 oob 上调用"看 §7 的三个条件；
- "能不能迁到 LKL"看有没有共享状态，以及是否依赖单核原语。

还有一类是**内联原语**：`atomic_*`、`raw_copy_from_user`、`queued_spin_lock` 的快路径。它们源码来自 Linux、机器码在 EVL 里，本身对 oob 安全，但不能改调 LKL（LKL 的版本是单核的），保持原样。

### 9.3 修正后的规则

> **R1（oob 限制）**：oob 上只能调用对 oob 安全的代码：E 域代码（含内联原语）、LKL 里的无状态函数、留在 Linux 的门。不是"oob 不能调用 Linux 函数"。
>
> **R2（inband 自由）**：inband 上可以跨域调用 Linux，但持有 oob 也用的锁时不行。
>
> **R3（拆分）**：不允许在 oob 上同步地同时修改 EVL 私有数据和 Linux 共享数据；这样的逻辑必须拆成"oob 半边 + 交接对象 + inband 半边"，或写成 stage 多态函数，oob 分支只走门。

---

## 10. 实证：proxy.c 和 work.c 的 Linux 调用

### 10.1 oob 上执行的

| Linux 函数 / 访问 | 调用点 | 读写的共享状态 | inband 会同时改吗 | 为什么正确 |
|---|---|---|---|---|
| `fdget` / `fput`（`evl_get_file`） | `file.c:37` | fd 表、`file->f_count` | 会 | 无锁查表 + 原子加引用；Dovetail 补丁（§7.3） |
| 读 `filp->private_data` | `proxy.c:401` 等 | `struct file` 字段 | 不会 | 初始化后不变 |
| 读 `filp->f_flags` | `proxy.c:340,410,435` | 同上 | 会（`fcntl`） | 单字，可能读到旧值 |
| 读 `proxy->filp->f_op->oob_poll` | `proxy.c:487-488` | 目标文件的 `f_op` | 不会 | 初始化后不变 |
| `raw_copy_from_user` / `raw_copy_to_user` | `proxy.c:200,372` | 用户页表、用户内存 | 会 | MMU 查页表项是原子的；缺页由 Dovetail 截获降级 |
| `memset` | `proxy.c:203` | 无 | — | 纯函数 |
| hard lock 及其慢路径 | `proxy.c:181` 等 | CPU 中断标志、`qnodes` | `qnodes` 会 | per-CPU 嵌套 |
| `irq_work_queue` | `work.c:101` | `irq_work.flags`、`raised_list`、中断日志 | 会 | 无锁认领和入队；关硬件中断记账 |
| `running_inband()`、`evl_current()` | `proxy.c:218` 等 | `preempt_count`、`current_task`、`oob_state` | 会 | per-CPU 单字；inband 切换任务时关硬件中断 |
| EVL 运行队列寻址 | `proxy.c:412,442` | `this_cpu_off` | 不会 | 初始化后不变 |
| 读时钟 | `evl_wait_flag` 带超时 | `tk_fast_mono` | 会 | latch |

### 10.2 只在 inband 上执行的

| Linux 函数 | 调用点 | 读写的共享状态 | 为什么正确 |
|---|---|---|---|
| `mutex_lock` / `unlock`（`worker_lock`、`f_pos_lock`），读写 `f_pos` | `proxy.c:83,90-92,108,120` | mutex、目标文件位置 | oob 不碰 |
| `kernel_write` / `kernel_read` | `proxy.c:106,265` | VFS、页缓存、inode 锁、sb 冻结计数、fsnotify、IO 统计 | oob 不碰，这正是 proxy 存在的意义 |
| `wake_up`、`wait_event_interruptible`、`poll_wait` | `proxy.c:139,296,507,554-566` | 等待队列、Linux 运行队列 | oob 不碰；`do_proxy_read` 在 oob 上读完只调 `evl_schedule`（`proxy.c:393`） |
| `queue_work_on` | `work.c:45` | PENDING 位、workqueue pool、kworker 唤醒 | oob 不碰，这正是 oob 要先跳 irq_work 的原因 |
| `flush_work` | `work.c:121` | 同上 | inband 分支；oob 分支改用 `evl_wait_flag` |
| `kzalloc`、`kfree`、`kvfree_call_rcu` | `proxy.c:633,710,776` | slab per-CPU 缓存、RCU 回调队列 | oob 从不分配或释放 |
| `alloc_workqueue`、`destroy_workqueue`、`cancel_work_sync`、`irq_work_sync` | `init_ring`、`destroy_ring` | workqueue 全局链表、irq_work 的 flags | 只在 inband；`dispose` 在引用计数归零后才调用，这时已没有 oob 用户 |
| `fget`、`fput`、`_copy_from_user`、`get_file`、`call_mmap` | `proxy.c:706,772,700,580,590` | fd 表、引用、mm | 生命周期和 mmap 路径，只在 inband |
| 各种 `init` | `init_ring`、`evl_init_work` | 只初始化 EVL 自己的内存 | 发布前没有并发 |

### 10.3 inband 写、oob 读：inband 一侧必须遵守 oob 的协议

| 调用 | 调用点 | 怎么保证正确 |
|---|---|---|
| `evl_raise_flag(&ring->oob_wait)` | `proxy.c:138`、`work.c:33` | inband 也拿 hard lock `wchan.lock`；唤醒后经 `run_oob_call` 进入 oob 调度 |
| `fillsz`、`reqsz`、`on_eof` 的原子操作 | `proxy.c:116,283,438,446` | 两边都用原子操作 |
| `in->on_error` | `proxy.c:347` | 单字 |

### 10.4 结论

proxy 和 work 在 oob 上碰到的 Linux 共享状态，全部落在 §8.3 的几种协议里。VFS、页缓存、workqueue pool、运行队列、等待队列、slab、RCU 这些重量级状态，oob 一个都不碰。proxy 的整个设计就是为了做到这一点：oob 只往 ring 里写、挂一个 irq_work，其余都推到 inband。

---

## 11. 共享状态的三类处理

### 11.1 出发点

由于正确性的限制，与 oob 共享的字段和相关操作都比较简单：基本是单字、原子操作或双副本。这启发了一个想法：**能不能把这些共享字段放到 EVL 的地址空间（EVL 与 LKL 共享一个地址空间），Linux 要修改它们必须调用 EVL 提供的接口？**

这个想法对一部分字段成立。迁移的代价不由协议复杂度决定，而由 **Linux 有多少地方、多频繁地访问这个字段**决定：`f_count` 的协议最简单，可 Linux 每次系统调用都要改它。

### 11.2 怎么算高频、低频

三条，**都满足才算低频**：

1. **EVL 能否接受读到稍旧的值？**这条决定能不能用镜像，比频率更根本。`f_flags`、时间、`cpu_online_mask` 可以接受；引用计数和锁只能有一份，不能做镜像。
2. **Linux 里有多少处写它？**决定改动量。proxy 文件的 `f_flags` 在 open 之后只有 `setfl`（`fs/fcntl.c:83`）和 `ioctl_fionbio`（`fs/ioctl.c:357-359`）两处写；`preempt_count` 被每个 `spin_lock`、`preempt_disable` 改，展开后成千上万处。
3. **推送开销占多少 CPU？**按"写的频率 × 一次跨域开销"估算：

| 字段 | 写的频率 | 加一次跨域（按约 100ns 估算） | 结论 |
|---|---|---|---|
| `tk_fast_mono` | 每个 tick，HZ=1000 时每秒 1000 次 | 约 0.01% CPU | 低频 |
| `f_flags` | 只在 `fcntl` 时 | 可以忽略 | 低频 |
| `f_count` | 每次系统调用 `fget` + `fput`，每秒可达上百万次 | 约 20% CPU | 高频 |
| `preempt_count` | 每次加锁解锁，原操作只要 1 个周期 | 不可接受 | 高频 |

### 11.3 第一类：初始化后不变的字段，EVL 存快照

例子：`f_op`、`private_data`、`f_oob_state.data`、`this_cpu_off`。发布时拷一份到 EVL，之后只读自己的副本，Linux 不需要调任何接口。EVL 已经在这么做：`proxy->filp` 是创建时存下的，`evl_file` 里也存着 `filp`。

### 11.4 第二类：Linux 低频写的字段，EVL 存镜像

- EVL 空间里放一份副本；Linux 每次写自己那份时，顺便调用 EVL 的接口同步更新副本；EVL 执行时只参考副本的值。
- EVL 需要修改时，改自己的副本，然后通过 irq_work 交给 inband，让 Linux 去改它那份。
- 不是把字段从 Linux 结构体里"拿出来"：`struct file` 由 Linux 分配，`f_flags` 被大量内联代码按偏移访问，挪走字段要改布局和所有访问点。现实的做法是保留原字段，另建镜像。副本会比原字段晚一点更新，对这类字段来说本来就可以接受。

### 11.5 第三类：Linux 高频写的字段

**先分出"EVL 只是借用 Linux 服务"的状态，另建一份自己的。**以 irq_work 为例，EVL 借用它只是为了"回到 inband 时执行一个回调"：

```
现在：
oob:    irq_work_queue(&work->irq_work)
        → 挂进 Linux 的 per-CPU raised_list        ← 全 Linux 几十个子系统共用
        → irq_post_inband(Linux 的 irq_work 中断)
inband: irq_work_run() → __evl_do_irq_work()

另建一份：
oob:    llist_add(&work->node, this_cpu_ptr(&evl_inband_list))   ← EVL 自己的链表
        → irq_post_inband(evl_virq)                              ← EVL 自己的虚拟中断
inband: evl_virq 的处理函数（EVL 代码）取下链表，逐个调用回调
```

虚拟中断用 Dovetail 的 `synthetic_irq_domain`（`kernel/irq/pipeline.c:37`），`drivers/evl/latmus.c` 已经在用。效果：共享状态从"全 Linux 共用的 `raised_list`"缩成"Dovetail 中断日志里 `evl_virq` 那一位"，链表变成 EVL 私有。链表操作需要真正的 `cmpxchg`，不能交给单核的 LKL，由 EVL 自己写，几十行就够。同样的思路可用于：

| 借用的服务 | 另建的做法 |
|---|---|
| `qnodes` | EVL 的 hard lock 用自己的一组排队节点 |
| printk 缓冲区 | EVL 自己的日志环形缓冲区，在 inband 上转交给 printk |

**剩下的就是共享区。**这些状态本身就是两个 stage 之间的耦合机制，拆不开：

| 状态 | 位置 | 为什么移不出去 |
|---|---|---|
| `preempt_count` | `pcpu_hot` | Dovetail 的 stage 就存在它的几位里（`stage_level()` = `(preempt_count() & STAGE_MASK) >> STAGE_SHIFT`，`include/linux/preempt.h:129`），Linux 每次加锁也改同一个字 |
| `current_task` | `pcpu_hot` | Linux 每次切换任务都写；EVL 靠它和 `oob_state` 找到当前线程 |
| `thread_info->oob_state` | 每个 task | EVL 线程与 Linux task 互相指向，"一个线程两种身份"的实现 |
| Dovetail 中断日志和 stall 位 | per-CPU | 两个 stage 交接中断用的就是它 |
| `f_count`、fd 表 | 每个文件、每个进程 | 引用计数只能有一份；oob 系统调用要通过 fd 找到文件 |

### 11.6 共享区不是保护机制

共享区没法同时满足"Linux 改不坏它"和"Linux 写它很便宜"。把两种保护分开看：

| 保护什么 | 含义 | 谁能做到 |
|---|---|---|
| 防误写（空间保护） | 不该写的代码写不进来，如 Linux 某处的野指针 | 硬件：页表、PKS |
| 防写错（语义保护） | 有权写的代码写进去的值是对的 | 只有接收方自己检查 |

- 第一、二类走 EVL 的接口，两种保护都有：字段在 EVL 空间，Linux 写不进来；Linux 推过来的值 EVL 可以检查。代价是每次写都跨一次域，所以只适合低频。
- 高频字段做不到。即使有"便宜的写"（比如用 PKS 临时开放权限），也只能防误写，防不了有权写的代码本身有 bug；而 `preempt_count` 的写散布在成千上万个内联点上，连开放权限的地方都没有。

所以**共享区的真实含义是：EVL 不得不信任的、Linux 可以直接写的最小内存集合**。它实际在 Linux 地址空间里。把它单独划出来，是为了：

1. **列清楚、缩到最小**：明确 EVL 的隔离到底在哪里漏风，这本身就是一个结论；
2. **在 EVL 读时尽量校验**：例如 `evl_current()` 拿到线程对象后，核对它指回的 `task_struct` 是不是 `current`；
3. **对写操作集中的部分加防误写**：Dovetail 中断日志和 `oob_state` 的写主要在 Dovetail 代码里，可以让 Linux 其余代码对它只读。`preempt_count`、`current_task`、`f_count` 的写散布在整个 Linux 里，只能信任或读时校验。

前两类之所以不放进共享区，是因为它们能以低代价移出信任范围；放进去等于白白扩大 TCB。原则是：**能移出信任范围的，用接口移出去；实在移不出去的，才放进共享区，而且共享区越小越好。**

另外，按 §4.4 的保险箱模型，真正值得保护的是 **EVL 的正确性依赖、而 Linux 会写的字段**。`f_op`、`f_flags` 读错了最多是行为出错，用副本解决；`f_count` 被改坏可能导致 EVL 用到已释放的文件，`preempt_count`、`current_task` 决定 EVL 以为自己在哪个 stage、哪个线程。最后这几项正好都在共享区里，所以最终需要特别处理的是一个很小的集合：引用计数，以及 stage 和线程的标识。

### 11.7 最终的地址空间图景

| 区域 | 内容 | 谁能写 |
|---|---|---|
| EVL 私有（EVL + LKL） | EVL 对象、LKL 的段和内存池、第一类快照、第二类镜像、另建的服务（EVL 的 inband 交接链表、qnodes、日志缓冲区） | EVL；Linux 只能通过 EVL 接口推送镜像更新 |
| Linux 私有 | VFS、页缓存、workqueue、slab、运行队列…… | Linux；EVL 按保险箱模型可以读，但 oob 只读协议安全的字段 |
| 共享区 | 每个 CPU 的 `pcpu_hot` 和中断日志、每个 task 的 `oob_state`、文件引用计数 | 两边都直接写；EVL 信任它，尽量读时校验 |

`preempt_count` 和 `current_task` 都在 `pcpu_hot` 里，而 `pcpu_hot` 正好是每个 CPU 一个 64 字节缓存行，单独映射起来很自然。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-7]

---

## 12. 下一步试验

两个试验分别验证第一类（快照）和第二类（镜像）。

> **2026-11-04 更新**：§12 的三项（试验 1、试验 2，以及 §12.4 的 irq_work 另建）已经实现，记录在 [impl-shared-state/](impl-shared-state/)。其中试验 1 在实现后改为**删除** `proxy_oob_poll` 对目标 `oob_poll` 的转发，而不是快照：这一步转发在语义上不成立（proxy 的读写从不走目标的 oob 路径），而且在 socket 场景下会调用乱指的函数指针，详见 [impl-shared-state/01](impl-shared-state/01-drop-oob-poll-forward.md)。下面保留原计划备查。

### 12.1 试验 1：目标文件的 `f_op->oob_poll` 改为快照（不改 Linux）

1. `struct evl_proxy`（`proxy.c:57`）加字段：
   ```c
   __poll_t (*target_oob_poll)(struct file *filp, struct oob_poll_wait *wait);
   ```
2. `proxy_factory_build` 在 `fget` 之后、元素发布之前（`proxy.c:706` 之后）拷一次：
   ```c
   proxy->target_oob_poll = filp->f_op->oob_poll;
   ```
3. `proxy_oob_poll` 改为读副本：
   ```c
   if (proxy->target_oob_poll)
   	ret |= proxy->target_oob_poll(filp, wait);
   ```

注意：前提是 `f_op` 在 open 之后不再变（`replace_fops` 都发生在 open 返回之前）；不要顺手改第一个参数，[11](11-proxy-linux-deps.md) §5 怀疑它应该是 `proxy->filp`，但尚未验证，修 bug 单独提交。

### 12.2 试验 2：proxy 文件自己的 `O_NONBLOCK` 改为镜像（改 Linux 两处）

oob 路径读的是 proxy 文件**自己**的 `f_flags`，在 `proxy.c:340,410,435`。整个 EVL 有 22 处读 `O_NONBLOCK`，分布在 9 个文件里，都只关心这一位。

1. **镜像放在 `struct evl_file`**（`include/evl/file.h:18`），它与 `struct file` 一一对应（同一个 proxy 元素可被打开多次，各有各的 flags）：
   ```c
   struct evl_file {
   	...
   	bool nonblock;          /* O_NONBLOCK 的镜像 */
   };

   static inline bool evl_file_nonblock(struct evl_file *efilp)
   {
   	return READ_ONCE(efilp->nonblock);
   }
   ```
2. **初始化**：`evl_open_file`（`file.c:58`）里用 open 时的 `f_flags` 设初值。
3. **Linux 推送**：在 `fs/fcntl.c:83` 之后和 `fs/ioctl.c:357-359` 之后各加：
   ```c
   if (filp->f_oob_state.data)
   	dovetail_file_flags_changed(filp);
   ```
   由 Dovetail 声明为弱函数，EVL 实现为 `WRITE_ONCE(efilp->nonblock, !!(filp->f_flags & O_NONBLOCK))`。不能用现成的 `f_op->check_flags`：它只传 `arg` 不传 `filp`（`fcntl.c:67-68`），而且在真正赋值前就被调用。
4. **oob 侧读镜像**：从 `filp->private_data` 找到 `evl_file_binding`，其第一个成员就是 `efile`（`include/evl/file.h:25-26`），把三处 `filp->f_flags & O_NONBLOCK` 换成 `evl_file_nonblock(efilp)`。

说明：找 `evl_file` 仍要读 `private_data`，属于初始化后不变的字段。要彻底不读，需要让 oob 系统调用分发直接把 `evl_file` 传给处理函数（`syscall.c` 里已经拿到 `efilp`），改动面大，不在这次试验里。

### 12.3 验证

1. **静态**：用 `pahole -C file`（或打印 `offsetof`）拿到 `f_op`、`f_flags` 的偏移，`objdump -dr` 对比改前改后的 `proxy_oob_poll`、`proxy_oob_write`、`proxy_oob_read`、`do_proxy_read`，确认不再有这些偏移的读取。
2. **功能**：QEMU 里跑 libevl 的 `tests/proxy-poll.c`、`proxy-eventfd.c`、`proxy-pipe.c`、`proxy-echo.c`。
3. **镜像专项**：创建小 proxy；`fcntl(F_SETFL, O_NONBLOCK)` 后写满 ring，再 `oob_write` 应立刻返回 `-EAGAIN`；`ioctl(FIONBIO, 0)` 后 `oob_write` 应阻塞，由另一线程排空 ring 唤醒。覆盖两个推送点。

### 12.4 之后

试验 1 先做，用来跑通静态检查流程。之后可以把 irq_work 另建一份（§11.5）作为第三类的第一个试点：需要改 `evl_call_inband_from` 和 `__evl_put_element`，改动不大，收益清楚。

---

## 13. 与 SoK 的对应，以及论文要点

| SoK | 本文 |
|---|---|
| 主体选择 | 以代码为中心（E / L / D）+ stage 约束的混合（§4） |
| P1 策略定义 | §5 哪些函数要处理；§7–8 函数和字段的判据；§9 迁移判定 |
| P2 隔间抽象 | 门的集合（§9.2 右上格）；交接对象（ring + `fillsz` + irq_work）；同步调用 + 共享内存 |
| P3 隔离机制 | §11 三类处理与共享区；先静态检查，硬件隔离后评估 |
| 威胁模型 | 保险箱；核心属性是时间可用性（SoK 指出学术界很少关注可用性） |

两个动机要分开写：

- **实时性**：只与 oob 一侧有关。oob 一侧对 Linux 的依赖本来就很少（门 + 内联原语），主要工作是用静态检查确认这一点，并处理内联字段访问。
- **可移植性和体量**（原则 2、3）：主要工作量在 inband 一侧，与实时性无关。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-8]

---

## 14. 待讨论和未验证的问题

1. **tracepoint 的 RCU 生命周期**（未验证）：probe 数组注销后经 RCU 宽限期释放，oob 读者可能不被 RCU 看见，从而在使用中被释放。需要查 Dovetail 是否已处理。
2. **EVL 的 oob 读写入口没有 `access_ok`**（未验证）：`do_proxy_write` 直接 `raw_copy_from_user`，`__thread_oob_ioctl` 用 `__get_user_nocheck_4`。如果属实是安全问题。
3. **`proxy_oob_poll` 传错 `filp`**（`proxy.c:488`）：读代码确认，socket 场景下会调用乱指的函数指针；转发已删除，见 [impl-shared-state/01](impl-shared-state/01-drop-oob-poll-forward.md)，尚未运行复现。
4. **`relay_output` 出错后可能卡住**（未验证）。
5. **stage 分支的自动识别**：`running_inband()` 是内联的，二进制里只是对 `preempt_count` 的位测试，可能需要源码级分析（如 clang 插件）。
6. **`f_op` 在 open 之后不变**这个前提，要在更多驱动上确认（试验 1 改为删除转发后，proxy 不再依赖它）。
7. **E-ib 数据**（如 `rdoff`、`worker_lock`）值不值得迁到 LKL：迁过去要用 LKL 的 mutex，进而需要 LKL 调度器在跑。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C13-9]
8. **共享区的读时校验**具体能做哪些，`pcpu_hot` 单独映射的可行性。
9. **门的白名单**：Dovetail 已有的 oob 安全函数没有集中列表，需要从补丁里整理。
