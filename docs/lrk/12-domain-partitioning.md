# EVL / Linux 的域划分：以代码为中心 + stage 约束的混合模型

本文回答"函数和数据应该按什么标准划分到不同的域"。理论框架来自 SoK: Software Compartmentalization（S&P'25，笔记见 [papers/SoK_Software_Compartmentalization_笔记.md](papers/SoK_Software_Compartmentalization_笔记.md)），例子全部取自 `kernel/evl/proxy.c` 和 `kernel/evl/work.c`。

与其他文档的关系：
- [02](02-object-classification.md) 的对象三分类、[03](03-function-levels.md) 的 L0/L1/L2 分级是本文的下层判据，本文把它们放进统一框架。
- [11](11-proxy-linux-deps.md) §1 是本文的前身（初版定义及其 10 个反例），§2 的依赖清单是本文例子的数据来源。

---

## 0. 为什么一直想不清楚

"函数和数据怎么分域"这个问题里其实混着三个不同的问题：

| 问题 | 关心什么 | 对应 SoK |
|---|---|---|
| Q1 实时正确性 | 这段代码在 oob 上执行会不会出错、会不会等 Linux | P1 策略（的约束部分） |
| Q2 可迁移性 | 这个依赖能不能交给 EVL 自己或 LKL | P1 策略（的归属部分） |
| Q3 隔离 | 代码和数据装进哪个地址空间，跨域访问要不要校验 | P2 抽象 + P3 机制 |

初版定义（[11](11-proxy-linux-deps.md) §1.1）用"定义在哪"划函数、用"谁创建"划对象，试图一次回答三个问题，结果每个问题都只答对一半：

- `raw_copy_from_user`、`queued_spin_lock` 定义在 Linux 头文件里，机器码却在 EVL 的目标文件里，按"定义在哪"无法装进 Linux 地址空间（Q3 失败）。
- `proxy` 和 `bufmem` 由 Linux 的 `kzalloc` 创建（`proxy.c:633,710`），按"谁创建"是 Linux 对象，可它们只被 EVL 使用、生命周期由 EVL 控制（Q2 失败）。
- `do_proxy_write` 是 EVL 函数，但它在 inband 上会同步调用 `relay_output`，进而调 `kernel_write`（`proxy.c:218-221`）；按"EVL 函数属于 EVL 域"，看不出这条路径在 oob 上是不允许的（Q1 失败）。

本文的做法：**先用 SoK 的框架选定"主体"是什么，再分别给出数据和函数的判据，地址空间放到最后。**

> **2026-11-06 注**：本文默认"可信 = oob、不可信 = inband"，把"只在 inband 运行的 EVL 函数"一律视作 Linux 函数。这对时效性成立，对完整性不成立：写可信对象的 inband 代码（工厂、`.release` 等）也是可信代码。修正后的模型见 [14-trust-domain-vs-stage.md](14-trust-domain-vs-stage.md)，本文需要修正的地方已用"→ 见 doc 14 §12"标出，原文未改。

---

## 1. SoK 的框架

### 1.1 三个概念

| 概念 | SoK 定义 | 在本课题里 |
|---|---|---|
| 主体（subject） | 计算单元：一段指令、一个线程 | 见 §4：（代码区域，stage）二元组 |
| 客体（object） | 权限施加的单元：内存字节、文件 | EVL/Linux 的数据结构及其字段 |
| 保护域（protection domain） | 权限集合相同的最大主体集合 | EVL 域、Linux 域，Dovetail 作为仲裁层 |

隔间化 =（1）把程序划分成多个保护域的**策略** +（2）该策略的运行时**强制执行**。本课题当前阶段主要做（1），（2）先用静态检查代替（§7）。

### 1.2 主体选择：三种划分方法

| 方法 | 主体 | 域何时确定 | 适合 |
|---|---|---|---|
| 以代码为中心（code-centric / spatial） | 代码区域 | 编译时，静态 | 不信任对象落在具体代码单元上（第三方库、驱动） |
| 以数据为中心（data-centric / temporal） | 执行实例（线程、进程、请求） | 运行时，动态 | 同一段代码服务多条互不信任的数据流（Web 服务器 worker） |
| 混合（hybrid） | 被限定在某段代码区域里的执行实例 | 两者兼有 | 需要同时约束"谁在跑"和"能跑哪些代码" |

> **术语陷阱**：SoK 的"以数据为中心"指**按执行实例划分**，不是"按数据归属划分"。以代码为中心的方案同样要决定每个域能访问哪些数据，这叫**客体归属分析**，属于以代码为中心框架内部的工作。本文 §5 的"按同步规则给数据分域"就是客体归属分析，不是 SoK 意义上的以数据为中心。

### 1.3 P1 / P2 / P3

| 层 | 问题 | 本文对应 |
|---|---|---|
| P1 策略定义 | 主体、客体、权限是什么 | §4 主体、§5 数据与函数的判据 |
| P2 隔间抽象 | 跨域调用（CALL/RETURN）、跨域共享（ASSIGN）怎么表达 | §6 门与交接对象 |
| P3 隔离机制 | 运行时怎么强制 | §7 先静态检查，硬件隔离后评估 |

SoK Insight 10：三层的决策互相影响，必须统筹。本课题也是如此：P1 中"oob 只能调用门"这条规则，直接决定了 P2 门的集合，也决定了 P3 的开销集中在哪里。

### 1.4 威胁模型

SoK 把信任模型分为沙箱、保险箱、相互不信任三种。本课题是**保险箱**：

- 被保护的是 EVL 实时核心（及其 LKL 库），要保证它的**可用性**（实时性：最坏延迟有界）和**完整性**（状态不被 Linux 破坏）。
- 不防 EVL 破坏 Linux，Linux 一侧不需要被保护。
- 机密性不是目标。

SoK Insight 8 和挑战 4 指出学术界很少关注可用性，而本课题最核心的属性恰恰是可用性中的"时间可用性"。这是与已有隔间化工作的主要区别。

---

## 2. 为什么不选以数据为中心

以数据为中心在本课题里的样子，就是"每个 EVL 线程一个 LKL 实例"：主体是 EVL 线程，每个线程一个域。它在两个层面都走不通。

### 2.1 语义：EVL 的核心状态不属于任何一个执行实例

以 proxy 输出方向为例，`struct proxy_ring`（`proxy.c:28`）的字段被多个执行实例同时访问：

| 字段 | 谁访问 |
|---|---|
| `wroff`、`reserved`、`nesting` | 所有写者线程（oob 或 inband），在 `do_proxy_write` 里 |
| `fillsz` | 写者线程加 kworker（`relay_output`） |
| `rdoff` | 只有 kworker |
| `bufmem` | 写者线程写入，kworker 通过 `kernel_write` 读出 |
| `oob_wait` | oob 写者在上面等待，kworker 唤醒它们（`proxy.c:138`） |

一个 proxy 被一组线程共享，再加上一个不属于任何 EVL 线程的 kworker。`struct evl_work`（`include/evl/work.h:16`）也一样：`evl_call_inband_from` 在某个线程里把它挂上 irq_work，`__evl_do_irq_work` 在 irq_work 上下文里执行，`__evl_do_work` 在 kworker 里执行，三者不是同一个执行实例。

EVL 的运行队列、定时器、factory 名字表更是全局共享的。按执行实例划分的前提是"各实例之间基本没有共享状态"，这个前提不成立。

### 2.2 实现：LKL 是单例

- LKL 的全部全局状态（`lkl_ops`、`cpu`、`is_running` 以及整个内核的全局变量）是一份。blob 以 `-mcmodel=kernel` 非 PIC 编译，对 `.data`/`.bss` 用绝对地址访问，同一份代码无法指向不同的数据副本。
- 目前 blob 的 `.data` + `.bss` 约 2.1MB，`.text` + `.rodata` 约 3.2MB（`size -A build/linux-evl-qemu/kernel/evl/lkl_blob.o`）。多实例只能复制整个 blob，或者每次切换线程时切换页表。
- 每个 LKL 实例启动后还会自己创建 init、kthreadd、ksoftirqd、kworker 等宿主线程，"一个实例只被一个线程使用"本身也不成立。

结论：以数据为中心适合的场景是"多个实时应用之间互相隔离"，不是 EVL 核心内部的划分。

---

## 3. 为什么纯粹以代码为中心也不够

以代码为中心的优点很明显：

- 要防范的对象落在具体代码上：Linux 的锁、调度器、VFS 不能出现在实时路径上（SoK Insight 1）。
- blob 在链接时静态合入，域在编译时确定，与以代码为中心的静态隔间一致。
- SoK Insight 6：所有自动化的策略定义方法都是以代码为中心的。[11](11-proxy-linux-deps.md) 用的 `nm -u` + `objdump -dr` 方法可以直接扩展成自动化工具。

但它漏掉了一个维度：**Dovetail 的 stage 是时间维度**。同一段 EVL 代码可以在两个 stage 上执行，而两个 stage 下允许做的事完全不同。proxy.c 和 work.c 里有三个典型例子，它们都是"同一个函数、在函数内部按 stage 分叉"：

**例 1：`do_proxy_write`（`proxy.c:159`）**

```c
if (n == rsvd) { /* empty -> non-empty transition */
	if (running_inband()) {                       // :218
		raw_spin_unlock_irqrestore(&ring->lock, flags);
		relay_output(proxy);                      // 同步调用，内含 kernel_write
		return ret;
	}
	evl_call_inband_from(&ring->relay_work, ring->wq);  // :223，oob 只能走门
}
```

**例 2：`evl_call_inband_sync_from`（`work.c:111`）**

```c
if (unlikely(running_inband())) {                 // :118
	evl_call_inband_from(work, wq);
	flush_work(&work->wq_work);                   // inband：用 Linux 的同步原语等待
	return sync_work->result;
}
evl_call_inband_from(work, wq);
return evl_wait_flag(&sync_work->done) ?: ...;    // oob：用 EVL 的 evl_flag 等待
```

**例 3：`__evl_put_element`（`factory.c:253`）**

```c
if (unlikely(running_oob() || hard_irqs_disabled())) {   // :286
	init_irq_work(&e->irq_work, do_put_element_irq);
	irq_work_queue(&e->irq_work);                 // oob：交给 inband 去释放
} else {
	... __do_put_element(e);                      // inband：直接释放（会调 Linux 的 kfree 等）
}
```

如果只按代码划分，这三个函数都"属于 EVL 域"，规则无法表达"inband 分支可以调 Linux、oob 分支不可以"。同理，`relay_output`、`init_ring`、`proxy_factory_build` 都是 EVL 代码，但只在 inband 上运行，自由地调用 `mutex_lock`、`kernel_write`、`kzalloc`、`alloc_workqueue`，这完全合法，也不影响实时性。纯代码划分会把它们误判为"EVL 域对 Linux 的违规依赖"。

**这三个例子还说明了一件事：EVL 的作者其实已经在非正式地遵守一条混合规则，即 oob 分支只用门，inband 分支随意。**本文的方案就是把这条隐含规则显式化。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-1]

---

## 4. 方案：主体 =（代码区域，stage）

### 4.1 静态的代码域

| 代码域 | 内容 | 说明 |
|---|---|---|
| **E**（EVL 域） | `kernel/evl/`、`drivers/evl/`、LKL blob | LKL 是 E 域的私有库。helper 迁移的本质就是把"E → Linux 跨域调用"变成"E → LKL 域内调用" |
| **L**（Linux 域） | 宿主内核其余部分 | — |
| **D**（Dovetail） | `kernel/irq/pipeline.c`、`kernel/dovetail.c` 等，以及头文件中的 stage 原语 | 两域之间的仲裁层和门，属于 TCB |

内联函数和宏的归属按**机器码落在哪里**算：`atomic_add_return`、`raw_copy_from_user` 内联进 `proxy.o`，就是 E 域的代码。但这类代码内含 Linux 的知识（结构体布局、per-CPU 偏移、`current` 的位置），要单独标记为"内含 L 知识"，它们是 §7 隔离时最难处理的部分。

### 4.2 stage 维度

| stage | 谁在跑 | 允许的同步原语 |
|---|---|---|
| **oob** | EVL 线程的实时部分、oob 中断处理、EVL 定时器 | hard lock、硬关中断、原子操作、EVL 等待对象（`evl_flag`、`evl_mutex`……） |
| **inband** | Linux 一切代码，以及 EVL 在 inband 上的那一半 | 以上全部 + Linux 锁、RCU、虚拟关中断、睡眠 |

于是每个主体是一个二元组，例如：

| 主体 | 例子 |
|---|---|
| （E，oob） | `proxy_oob_write`、`do_proxy_write` 的 oob 分支、`evl_wait_flag` |
| （E，inband） | `relay_output`、`proxy_write`、`init_ring`、`__evl_do_irq_work`、`__evl_do_work` |
| （L，inband） | `kernel_write`、`queue_work`、`mutex_lock`、`alloc_workqueue` |
| （D，任意） | `irq_work_queue`（Dovetail 改造后可从 oob 调用）、`irq_post_inband`、`run_oob_call` |
| （L，oob） | **只允许对 oob 安全的函数**（§4.4），例如 `memset`、`queued_spin_lock_slowpath`；`queue_work`、`mutex_lock` 这类不安全的函数出现在 oob 上就是 bug |

### 4.3 混合约束（核心规则）

> **R1（oob 限制）**：stage 为 oob 的执行只能调用**对 oob 安全**的代码（定义见 §4.4）。这些代码分三种：
> 1. E 域代码，包括内联进 E 域的 Linux 原语（`atomic_*`、`raw_copy_from_user`）；
> 2. LKL 里的 L0 函数（[03](03-function-levels.md)）。LKL 是单核实例，它的原子操作和锁在多核 oob 上不成立，所以只有无状态的函数可以；
> 3. 留在 L 域、但对 oob 安全的函数，也就是**门**。其中无状态的（`memset`）原则上可以复制进 E 域；有 Linux 全局状态的（`irq_work_queue`）只能留在 L 域，由 D 域审核。
>
> R1 不是"oob 不能调用 Linux 函数"。判据是函数依赖的同步规则在 oob 上是否成立，而不是函数由谁编写。
>
> **R2（inband 自由）**：stage 为 inband 的执行，包括 E 域代码，可以跨域调用 L 域，不影响实时性；但仍需经过定义好的接口（§6），以便将来做隔离。唯一的限制是：**持有 oob 也会使用的锁（如 `hard_spinlock_t`）时，不能调用 L 域函数**，否则 oob 等这把锁就等于在等 Linux。`do_proxy_write` 的 inband 分支先放掉 `ring->lock` 再调用 `relay_output`（`proxy.c:219-220`），就是在遵守这条限制。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-2]、[C12-3]

这就是 SoK 意义上的混合划分："执行实例被限定在某段代码区域内"，只是限定条件由 stage 决定：在 oob 上被限定在"对 oob 安全"的代码里，在 inband 上不限定。

### 4.4 什么叫"对 oob 安全"

#### 4.4.1 oob 上其实一直在调用 Linux 函数

`proxy.o` 中，oob 路径（`proxy_oob_write` → `do_proxy_write`）调用的外部 Linux 函数（[11](11-proxy-linux-deps.md) §2.1）：

| 函数 | 在哪里被调用 | 是否安全 |
|---|---|---|
| `rep_movs_alternative` | `raw_copy_from_user` 内联展开后，CPU 不支持 FSRM 时的拷贝实现（`proxy.c:200`） | 安全 |
| `memset` | 拷贝出错时清零（`proxy.c:203`） | 安全 |
| `queued_spin_lock_slowpath` | `ring->lock` 竞争时的慢路径 | 安全 |
| `irq_work_queue` | `evl_call_inband_from`（`work.c:101`） | 安全（经 Dovetail 改造） |

而 inband 路径上的 `queue_work`（`work.c:45`）、`mutex_lock`（`proxy.c:83`）、`kernel_write`（`proxy.c:106`）都**不能**在 oob 上调用。区别在哪里？

#### 4.4.2 背景：oob 打断 inband 时发生了什么

Dovetail 把 Linux 的关中断改成了**虚拟的**：`local_irq_save`、`spin_lock_irqsave`、`raw_spin_lock_irqsave` 只设置一个 stall 位，告诉 Dovetail "暂时不要把中断交给 inband"，CPU 的硬件中断仍然开着。只有 `hard_local_irq_save` 和 `hard_spinlock_t` 才真正屏蔽硬件中断。

因此，当硬件中断到来、EVL 决定切换到某个 oob 线程时，inband 代码会**在任意位置**被冻结，包括它自以为"已经关了中断、拿着锁"的临界区内部。它要等到 oob 一侧没事可做了才能继续执行。

inband 代码之间的互斥仍然成立，因为 inband 的中断处理确实被推迟了；但 **inband 和 oob 之间不存在任何互斥**。一个函数能不能在 oob 上调用，取决于它是否依赖了这种 oob 并不遵守的互斥。

#### 4.4.3 条件一：不依赖被 oob 绕过的同步机制

**反例：`queue_work`**

`queue_work_on`（`kernel/workqueue.c:2415`）先 `local_irq_save`（虚拟），再在 `__queue_work` 里 `raw_spin_lock(&pool->lock)`（`:2334`），最后可能经 `kick_pool` 调用 `wake_up_process` 唤醒 kworker，这又要拿 Linux 运行队列的锁。假设 oob 线程 T 调用了它，在同一个 CPU 上：

| 时刻 | inband | oob |
|---|---|---|
| t0 | 某个任务调用 `queue_work`：`local_irq_save` 只设 stall 位，然后 `raw_spin_lock(&pool->lock)` 拿到锁 | — |
| t1 | 被冻结在临界区里 | 硬件定时器中断到达，Dovetail 立刻交给 EVL，EVL 切换到 oob 线程 T |
| t2 | 要等 oob 让出 CPU 才能继续，继续之后才能放锁 | T 调用 `queue_work`，`raw_spin_lock(&pool->lock)` 开始自旋 |
| t3 | 永远等不到 | 永远等不到 |

结果是**死锁**。如果持锁者在另一个 CPU 上，不会死锁，但 T 的等待时间取决于那边的 inband 什么时候放锁，而那边的 inband 同样可能被它所在 CPU 上的 oob 冻结，等待时间没有上界。

注意，这里的问题首先是**正确性**，其次才是延迟。哪怕 `queue_work` 平均只要 1 微秒，它在 oob 上也不能调用。

同样的道理适用于所有用 `preempt_disable`、`local_lock` 或虚拟关中断保护的 per-CPU 数据：oob 打断时，可能看到 inband 改了一半的状态。

**正例：`irq_work_queue`、`memset`、`rep_movs_alternative`**

- `memset`、`rep_movs_alternative` 只读写参数指向的内存，不碰任何共享状态，被打断、被重入都无所谓。
- `irq_work_queue`（`kernel/irq_work.c:116`）的两步都是无锁的：`irq_work_claim` 用 `atomic_fetch_or` 认领，`__irq_work_queue_local` 用 `llist_add`（`cmpxchg`）挂进 per-CPU 链表。在任意位置被打断、被同一个 CPU 上的另一个上下文重入，结果都正确。

后者其实是为 NMI 设计的：NMI 同样会在任意位置打断内核，包括关中断的临界区。**oob 打断 inband，本质上和 NMI 打断内核是同一种情况**，所以"对 NMI 安全"是判断"对 oob 安全"的一个好的近似。

**正例（不那么显然）：`queued_spin_lock_slowpath`**

它用到 per-CPU 的 MCS 节点 `qnodes`（`kernel/locking/qspinlock.c:109`），看起来是共享状态。但这组节点按嵌套层次设计，共 4 层（task、softirq、hardirq、NMI，`MAX_NODES` 在 `:70`），每被打断一层就用下一个节点；4 个都用完时退化为 `trylock` 自旋（`:413-415`）。所以 oob 打断后再进入慢路径，相当于多嵌套一层，仍然正确。

#### 4.4.4 条件二：不睡眠，不进入 Linux 调度，包括退出时隐式的调度

**反例：`mutex_lock`**

`relay_output` 用的 `worker_lock` 是 Linux 的 `mutex`。拿不到锁时会调用 `schedule()`，而 Linux 调度器只管理 inband 的任务，从 oob 调用它的结果是未定义的。

这也解释了为什么 proxy 的两个写入口用不同的方式等待：

| 入口 | stage | 缓冲区满时 |
|---|---|---|
| `proxy_write` | inband | `wait_event_interruptible(ring->inband_wait_w, ...)`，Linux 等待队列（`proxy.c:507`） |
| `proxy_oob_write` | oob | `evl_wait_flag(&ring->oob_wait)`，EVL 调度器（`proxy.c:412`） |

**容易漏掉的：退出时隐式的调度**

有些函数本身不睡眠，但在结尾"顺便"调度一下：
- `preempt_enable()` 发现有更高优先级的任务等着，就会调用 `preempt_schedule()`。
- `local_irq_restore()` 打开虚拟中断时，会重放这段时间积压的 inband 中断。

`irq_work_queue` 结尾正好有 `preempt_enable()`（`irq_work.c:125`），原版的 `arch_irq_work_raise` 还会发自中断 IPI。Dovetail 改了两处，它才变得对 oob 安全：
1. `irq_local_work_raise`（`kernel/irq/pipeline.c:1715`）改为 `hard_local_irq_save` 加 `irq_post_inband`：只在 inband 的中断日志里记一笔，等回到 inband 时再执行。
2. `preempt_schedule()` 开头加了 `if (likely(!running_inband() || !preemptible())) return;`（`kernel/sched/core.c:6987`）：在 oob 上直接返回，不调度。

所以 `irq_work_queue` 的 oob 安全性，是"Linux 原本就对 NMI 安全"加上"Dovetail 补上了退出路径"共同保证的。这正是 D 域作为"门的审核者"的含义。

#### 4.4.5 条件三：执行时间有上界

例如：
- 内存分配可能触发回收，所以 EVL 在 oob 上用自己的 `evl_heap`，不调 `kmalloc`。
- `raw_copy_from_user` 的耗时和拷贝长度成正比，上界由 `bufsz` 决定（最大 2^30）。这类"有上界，但上界由用户配置决定"的情况，要由使用者自己保证。

#### 4.4.6 小结：两个问题要分开问

| | 无状态 | 有 Linux 全局状态 |
|---|---|---|
| **对 oob 安全** | `memset`、`rep_movs_alternative`、`rb_insert_color`：可复制，交给 LKL 或编进 E 域，之后就不算跨域 | `irq_work_queue`、`queued_spin_lock_slowpath`：留在 L 域，**这就是门的集合**，要尽量小并经过审核 |
| **对 oob 不安全** | 少见，例如打开了调试钩子的版本 | `queue_work`、`mutex_lock`、`kernel_write`、`kmalloc`：只能在 inband 上调用 |

- "能不能在 oob 上调用"，看条件一到三。
- "属于哪个域、能不能迁移"，看有没有 Linux 全局状态（§5.3）。

### 4.5 第四类：运行时基础

有些东西两个域都必须用、而且无法拆分，单独列一类，由 D 域负责仲裁：

| 对象 | proxy/work 中的体现 |
|---|---|
| 栈 | EVL 函数在 inband 上跑在 Linux 分配的任务栈上；`relay_output` 的局部变量 `pos` 的地址被传给 `kernel_write`（`proxy.c:106`） |
| per-CPU 区 | irq_work 挂进的 `raised_list` 是 Linux 的 per-CPU 变量；EVL 的 `evl_runqueues` 也放在 Linux 的 per-CPU 区 |
| `current` / `thread_info` | `evl_current()` = `current_thread_info()->oob_state.thread`，`running_inband()` 读 `preempt_count` 中的 stage 位 |
| Dovetail 状态 | stall 位、`oob_state`、`f_oob_state` |

SoK §3.2 指出内核态隔间化"必须深入到启动、调度、内存管理和中断处理中"，这一类就是具体体现。

---

## 5. P1：数据与函数的判据

### 5.1 数据：按"被哪些主体访问、受什么同步规则保护"分类

数据的域不看谁分配、不看类型定义在哪，看两件事：**哪些主体会访问它**，以及**用什么同步机制保证一致性**。

| 类别 | 判据 | 同步要求 |
|---|---|---|
| **E-oob** | 有（E，oob）主体访问 | 必须用 oob 允许的同步原语 |
| **E-ib** | 只有（E，inband）主体访问 | 可以用 Linux 的同步原语 |
| **边界（B）** | 被两个 stage 访问，或被 E 和 L 两个代码域访问 | 必须有明确的交接协议（原子操作、无锁结构、门） |
| **L** | 只有 L 域代码访问，E 域只持有句柄 | Linux 自己负责 |

**proxy 与 work 的数据逐个归类：**

| 对象 / 字段 | 访问者 | 同步 | 类别 | 备注 |
|---|---|---|---|---|
| `ring->wroff`、`reserved`、`nesting` | 写者，oob 或 inband | `ring->lock`（`hard_spinlock_t`） | E-oob | hard lock 在两个 stage 都成立 |
| `ring->rdoff` | 只有 `relay_output`（kworker） | `ring->worker_lock`（Linux `mutex`） | **E-ib** | EVL 私有、但用 Linux 锁，合法，因为不会在 oob 上被访问 |
| `ring->fillsz` | 写者（`:214` `atomic_add_return`），kworker（`:116` `atomic_sub_return`） | 原子操作 | **B** | 两个 stage 之间的交接计数 |
| `ring->bufmem` 的内容 | 写者 `raw_copy_from_user` 写入（`:200`），`kernel_write` 读出（`:106`） | 由 `fillsz` 协议保护 | **B** | 跨代码域的共享内存：L 域的 `kernel_write` 通过指针读 E 域内存 |
| `ring->oob_wait`（`evl_flag`） | oob 写者等待（`:412`），kworker 唤醒（`:138`） | `wchan.lock` hard lock | E-oob | inband 侧唤醒要经过门（§6.2） |
| `ring->inband_wait_w`（`wait_queue_head_t`） | `proxy_write` 等待（`:507`），kworker `wake_up`（`:139`） | Linux 等待队列锁 | E-ib（实例），L 规则 | 嵌在 EVL 对象里的 Linux 原语，只在 inband 使用 |
| `ring->worker_lock`（`struct mutex`） | `relay_output` | Linux | E-ib（实例），L 规则 | 同上 |
| `relay_work.irq_work` | EVL 初始化，`irq_work_queue` 从 oob 挂进 per-CPU `raised_list`，`irq_work_run` 在 inband 取出 | `atomic_fetch_or` 认领 + 无锁 llist | **B** | 实例属于 E，挂进的容器属于 L；这是 oob → inband 的交接点 |
| `relay_work.wq_work` | `__evl_do_irq_work` 在 inband 调 `queue_work` 挂进 workqueue pool | `pool->lock`（Linux） | E-ib（实例），L 容器 | 只在 inband 被碰，所以不需要特别处理 |
| `relay_work.element` 引用计数 | `evl_call_inband_from` 加（任一 stage），`__evl_do_work` 减 | `refcount` 原子操作 | B | 计数降到 0 时按 stage 分叉（§3 例 3） |
| `sync_work->done`、`result` | kworker 写 `result` 并 `evl_raise_flag`（`work.c:32-33`），oob 等待者读 | `evl_flag` | **B** | inband → oob 的交接 |
| `ring->wq` 指向的 `workqueue_struct` | 只被 L 域的 workqueue 代码读写 | Linux | L | E 只持有不透明句柄 |
| `proxy->filp` 指向的 `struct file` | VFS 和 `kernel_write` | Linux | L | 但 `relay_output` 直接读写了 `f_mode`、`f_pos`、`f_pos_lock`（`:89-92,108,120`），是 E 域代码"内含 L 知识"的直接字段访问 |
| `proxy` 本体的内存 | E 域使用，最后由 `kfree_rcu`（`:776`）交给 L 域释放 | — | E-ib → L | 生命周期的最后一步跨域：L 域的 RCU 回调释放 E 域对象 |
| `proxy->poll_head` | EVL poll 机制 | EVL 锁 | E-oob | — |

几点观察：

1. **`kzalloc` 不决定归属**。`proxy`、`bufmem` 都由 Linux 分配，但只有 E 域使用，属于 E 域。分配器只是一个提供内存的服务；隔离之后，E 域需要自己的分配器（或者把 Linux 分配器当作门来调用，再把返回的内存"移交"给 E 域）。
2. **嵌入关系不决定归属**。同样嵌在 `proxy_ring` 里，`rdoff` 是 E-ib，`fillsz` 是 B，`inband_wait_w` 是 L 规则的 E-ib 实例。决定类别的是访问者集合，不是在内存里放在哪。
3. **E-ib 是一个必要的中间类别**。没有它，`rdoff` 和 `worker_lock` 要么被误判为违规（E 域数据用了 Linux 锁），要么被误判为 L 域对象。
4. **B 类数据就是边界本身**。proxy 的整个设计，可以看作是在构造一组最小的 B 类数据（`fillsz`、`bufmem`、`irq_work`、`oob_wait`），让 E-oob 和 L 两侧都不需要直接碰对方的数据。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-4]

### 5.2 函数：跟着它碰的数据走

| 函数碰的数据 | 函数类别 | 允许的 stage | 例子 |
|---|---|---|---|
| 只碰参数、栈、只读常量 | **可复制**（L0） | 任意 | `can_write_buffer`（`:153`）、`proxy_may_write`（`:70`）、`container_of`、`rb_insert_color` |
| 碰 E-oob 和 B，不碰 L | **E-oob 函数** | 任意 | `proxy_oob_write`（`:398`）、`do_proxy_write` 的 oob 分支、`evl_call_inband_from`（`work.c:86`） |
| 碰 E-ib、B、L | **桥函数** | 只能 inband | `relay_output`（`:75`）、`__evl_do_irq_work`（`work.c:37`）、`__evl_do_work`（`work.c:9`）、`__evl_do_sync_work`（`work.c:27`） |
| 只碰 L | **L 函数** | 只能 inband | `kernel_write`、`queue_work`、`mutex_lock`、`alloc_workqueue` |
| 生命周期管理（创建、销毁） | **桥函数** | 只能 inband | `init_ring`（`:624`）、`destroy_ring`（`:661`）、`proxy_factory_build`（`:676`）、`proxy_factory_dispose`（`:760`） |
| 两个 stage 都可能调用 | **stage 多态函数** | 任意，但内部按 stage 分叉 | `do_proxy_write`、`evl_call_inband_sync_from`、`__evl_put_element` |

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-5]

> **R3（拆分规则）**：不允许任何函数在 oob 上同步地同时修改 E-oob 数据和 L 数据。这样的逻辑必须拆成"oob 半边 + B 类交接对象 + inband 半边"，或者写成 stage 多态函数，oob 分支只走门。

proxy 的输出路径就是 R3 的标准拆法：

```
oob 半边                    交接对象（B）                 inband 半边
do_proxy_write  ──写──▶  bufmem + fillsz  ──读──▶  relay_output ──▶ kernel_write
      │                                                    │
      └──evl_call_inband_from──▶ relay_work.irq_work ──▶ __evl_do_irq_work ──▶ queue_work ──▶ __evl_do_work
                                                                                                │
      ◀──────────────── evl_raise_flag(&oob_wait)（经 run_oob_call）◀────────────────────────────┘
```

### 5.3 判定流程

对 E 域代码调用的每个 L 域函数 `f`：

1. **`f` 会被（E，oob）主体调用吗？**（看调用点是否在 oob 可达路径上，或在 stage 多态函数的 oob 分支里）
   - 会：`f` 必须对 oob 安全，即满足 §4.4 的三个条件：不依赖被 oob 绕过的同步机制（近似为对 NMI 安全），不睡眠也不在退出时进入 inband 调度或 irq 重放，执行时间有上界。不满足就是现有 bug，与 LKL 无关。
2. **`f` 是 L0（没有可变全局状态）吗？**
   - 是：**可复制**，交给 LKL 或直接编进 E 域。还要满足：原语在调用上下文里正确，嵌入结构体的布局一致（[09](09-case-oob-irq-work.md) 的第 3、4 条标准）。
3. **`f` 碰的状态（包括它把对象挂进去的容器）是 E 域私有的吗？**
   - 是：连同状态迁到 E 域。例子：factory 的名字哈希表和 `hash_lock`。
   - 反例：`irq_work_queue` 碰的 `raised_list` 是 L 域的 per-CPU 容器，不满足；`queue_work` 碰的 pool 同理。
4. **否则是 L 域的共享状态**：
   - 留在 L 域，只允许（E，inband）调用（R2）；
   - oob 需要时，经 D 域的门交接（R1）。

这四步与 [02](02-object-classification.md) 的三分类一一对应：第 2 步 = 第一类（可复制），第 3 步 = 第二类（交给 EVL），第 4 步 = 第三类。本文新增的是第 1 步（stage 约束）和 §5.1 的数据判据，它们为三分类提供了可操作的判定依据。

---

## 6. P2：门与交接对象

### 6.1 SoK 的五个原语在本课题中的对应

| SoK 原语 | 本课题 |
|---|---|
| CREATE / DESTROY | 代码域是静态的，在链接时确定，不需要运行时创建；stage 切换（`evl_switch_oob` / `evl_switch_inband`）可以看作"主体在两个约束集合之间转换" |
| CALL / RETURN | 跨代码域调用：E → L（`kernel_write`）、L → E（函数指针回调）、E → D（门）；同步语义 |
| ASSIGN | 跨域共享数据：B 类数据；共享内存语义（如 `bufmem`），没有拷贝 |

SoK Insight 7：改造遗留软件的趋势是"同步调用 + 共享内存"，代价是接口安全要显式处理。本课题为了兼容内核，同样选择同步 + 共享内存，所以 §6.4 的接口安全不可回避。

### 6.2 门的清单（proxy/work 涉及的部分）

**oob → inband（R1 允许的出口）**

| 门 | 调用点 | 机制 |
|---|---|---|
| `irq_work_queue` | `evl_call_inband_from`（`work.c:101`）、`__evl_put_element`（`factory.c:288`） | 原子认领 + 无锁 llist；Dovetail 的 `irq_local_work_raise` 用 `irq_post_inband` 记账，等回到 inband 再执行 |
| `fdget`（`evl_get_file` 内部） | oob 系统调用入口 | Dovetail 保证可从 oob 调用 |

**inband → oob**

| 门 | 调用点 | 机制 |
|---|---|---|
| `run_oob_call` | `evl_raise_flag` → `evl_schedule`（`relay_output:138`、`__evl_do_sync_work:33`） | 在 inband 上临时进入 oob stage 执行 `__evl_schedule`，让被唤醒的 oob 线程立即抢占 |

**L → E（反向回调，门必须是双向的）**

| 回调入口 | 由谁调用 | 进入的 E 函数 |
|---|---|---|
| `proxy_fops`（`:609`） | VFS（inband）/ Dovetail oob 系统调用（oob） | `proxy_write` / `proxy_oob_write` 等 |
| `irq_work` 的 `func` | `irq_work_run`（inband） | `__evl_do_irq_work` |
| `work_struct` 的 `func` | workqueue kworker（inband） | `__evl_do_work` → `relay_output_work` |
| `rcu_head` | RCU 回调（inband） | `kfree_rcu` 直接释放 E 域对象，不经过 E 函数 |

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-6]

### 6.3 线程模型

SoK 把线程模型分为正交模型（线程随 CALL 跨越域）和耦合模型（线程固定属于某个域）。本课题是正交模型，而且是双重正交：

- 线程随函数调用跨越代码域：EVL 线程在 inband 上调用 `proxy_write` 时，执行流从 L（VFS）进入 E，再回到 L（`wait_event_interruptible`）。
- 线程随 stage 切换跨越约束集合：同一个 EVL 线程既有 oob 部分，也有 inband 部分。

SoK 指出正交模型"需要正确更新线程状态，定义好 TLS 的行为"。在 Dovetail 里，这就是 `thread_info->oob_state`：它是 §4.5 运行时基础里最关键的一项。

### 6.4 接口安全

SoK Insight 9：认为接口安全与隔间抽象正交，是普遍误解。在 proxy 中已经能看到接口安全问题的雏形（[11](11-proxy-linux-deps.md) §5，均**未验证**）：

| 现象 | 对应的接口安全问题 |
|---|---|
| EVL 的 oob 读写入口不做 `access_ok`，`do_proxy_write` 直接 `raw_copy_from_user` | 用户内存是第三个域，跨域指针没有范围检查，可能成为混淆代理 |
| `proxy_oob_poll`（`:488`）把 proxy 自己的 `filp` 而非 `proxy->filp` 传给目标文件的 `oob_poll` | 跨域调用时传错了对象句柄 |
| `relay_output` 出错后丢弃数据、可能使后续写入停滞 | 跨域调用的错误语义没有约定 |

此外，`kernel_write(filp, ring->bufmem + rdoff, n, ppos)` 中，L 域函数会顺着两个指针读 E 域的 `bufmem`、写 E 域栈上的 `pos`。门不仅要检查"能不能调用"，还要检查"指针参数指向的内存是否在本次调用授予的范围内"。这正是 SoK 说的"对跨接口指针强制指向范围"。

---

## 7. P3：强制执行（先静态，后硬件）

### 7.1 第一阶段：静态检查

不做运行时隔离，用工具检查 R1–R3 是否被遵守：

1. 从 oob 入口（`oob_write`、`oob_read`、`oob_ioctl`、`oob_poll`，oob 中断处理，EVL 定时器回调）出发，沿调用图求出所有（E，oob）可达的函数。
2. 对 stage 多态函数，按 `running_inband()` / `running_oob()` 分支剪枝，只保留 oob 分支。
3. 在 oob 可达集合中，用 `nm -u` + `objdump -dr` 找出所有对 L 域符号的调用，与门白名单比对；超出白名单的就是 R1 违规。
4. 对内联进来的"内含 L 知识"的代码，单独列出字段访问（`f_pos`、`preempt_count`、`this_cpu_off`……）。

难点是间接调用（`f_op->oob_poll`、`handler_noreturn`），静态分析只能保守处理。SoK Insight 5 指出静态分析会导致权限过大，可以用动态跟踪（ftrace 记录 oob 上实际调用的 L 域符号）作为补充。

### 7.2 第二阶段：硬件隔离（待评估）

| 机制 | 用于本课题时的问题 |
|---|---|
| 两套页表 | 切换开销大；栈、per-CPU 区、`current` 必然跨域，需要在两套页表里同时映射 |
| PKS（内核态保护键） | 切换快，但属于 SoK 说的"有条件机制"：要防止 E 域以外的代码修改 PKRS；最多 16 个键 |
| 软件检查（SFI） | 内联的字段访问全部要插桩，oob 热路径开销不可接受 |

LKL 的内存边界清楚，有利于硬件隔离：blob 自身的段（链接时位置固定）加上通过 host_ops 一次性分配的内存池。困难集中在 §4.5 的运行时基础和"内含 L 知识"的内联代码：一旦硬件隔离生效，`current`、`preempt_count`、per-CPU 基址这些 oob 热路径上的访问都要改写成门调用，开销落在实时路径上。所以硬件隔离应在逻辑划分稳定后再评估，而不是一开始就做。

---

## 8. 走查：proxy 输出路径上的每一步

以一个 oob 线程调用 `oob_write(proxy_fd, buf, len)` 为例，每一步标出主体和触碰的数据类别：

| # | 步骤 | 主体 | 触碰的数据 | 规则检查 |
|---|---|---|---|---|
| 1 | Dovetail oob 系统调用入口，`evl_get_file` → `fdget` | （D，oob） | fd 表（L）、`f_oob_state`（运行时基础） | 门，合法 |
| 2 | `proxy_oob_write` → `do_proxy_write` | （E，oob） | `filp->private_data`、`f_flags`（L，内联字段访问） | 内含 L 知识，需标记 |
| 3 | 持 `ring->lock` 预留空间 | （E，oob） | `wroff`、`reserved`、`nesting`（E-oob） | hard lock，合法 |
| 4 | 放锁，`raw_copy_from_user` 拷贝到 `bufmem` | （E，oob） | 用户内存（第三域）、`bufmem`（B） | 缺 `access_ok`（未验证） |
| 5 | `atomic_add_return` 更新 `fillsz` | （E，oob） | `fillsz`（B） | 原子交接，合法 |
| 6 | 空变非空：`evl_call_inband_from` → `irq_work_queue` | （E→D，oob） | `relay_work.irq_work`（B）、per-CPU `raised_list`（运行时基础） | 门，合法 |
| 7 | 缓冲区满时 `evl_wait_flag(&ring->oob_wait)` 睡眠 | （E，oob） | `oob_wait`（E-oob） | 合法 |
| 8 | 回到 inband，`irq_work_run` → `__evl_do_irq_work` → `queue_work` | （L→E→L，inband） | `wq_work`（E-ib 实例，L 容器）、pool（L） | inband，合法 |
| 9 | kworker：`__evl_do_work` → `relay_output` | （E，inband） | `rdoff`（E-ib）、`worker_lock`（L 规则）、`f_pos`/`f_pos_lock`（L，直接字段访问） | 桥函数，合法；字段访问需标记 |
| 10 | `kernel_write(filp, bufmem + rdoff, n, ppos)` | （L，inband） | `struct file`（L）、`bufmem`（B，经指针）、栈上 `pos`（运行时基础） | 跨域指针，将来需范围检查 |
| 11 | `atomic_sub_return` 更新 `fillsz` | （E，inband） | `fillsz`（B） | 合法 |
| 12 | `evl_raise_flag(&oob_wait)` → `run_oob_call(__evl_schedule)` | （E→D，inband→oob） | `oob_wait`（E-oob） | inband → oob 门，合法；第 7 步的线程被唤醒 |
| 13 | `wake_up(&inband_wait_w)` | （L，inband） | `inband_wait_w`（L 规则） | 合法 |

oob 一侧（第 1–7 步）调用的非 E 域函数有两类：D 域审核过的门（`fdget`、`irq_work_queue`），以及无状态或按嵌套设计的 L 域函数（`rep_movs_alternative`、出错路径上的 `memset`、锁竞争时的 `queued_spin_lock_slowpath`），全部满足 §4.4 的三个条件；所有 Linux 函数（第 8–13 步）都在 inband 上。按本文的模型，proxy 的输出路径满足 R1–R3，需要处理的只有第 2、9 步的内联字段访问和第 4、10 步的跨域指针。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-7]

---

## 9. 总结

| 问题 | 结论 |
|---|---|
| 选哪种主体 | 混合：以代码为中心（E / L / D 三个静态代码域）+ stage 约束 |
| 为什么不用以数据为中心 | EVL 核心状态被所有执行实例共享；LKL 是单例，无法按线程复制 |
| 为什么纯代码划分不够 | 同一段 EVL 代码在两个 stage 下允许的操作不同；EVL 已经在用 stage 多态函数 |
| 数据怎么分 | 按访问者集合和同步规则：E-oob / E-ib / B / L，外加运行时基础 |
| 函数怎么分 | 跟着数据走：可复制 / E-oob / 桥函数 / L / stage 多态 |
| 核心规则 | R1 oob 只能调用对 oob 安全的代码（不是"不能调 Linux"）；R2 inband 可跨域，但持有 oob 也用的锁时不行；R3 跨 E-oob 和 L 的逻辑必须拆分 |
| 什么叫对 oob 安全 | 不依赖被 oob 绕过的同步机制（≈ 对 NMI 安全）；不睡眠、退出时不进入 inband 调度；执行时间有上界 |
| 怎么强制 | 先静态检查调用图和符号；硬件隔离等逻辑划分稳定后再评估 |
| 与 SoK 的对应 | P1 = §4–5，P2 = §6，P3 = §7；威胁模型是保险箱，核心属性是时间可用性 |

## 10. 待讨论

1. **E-ib 数据算不算"交给 EVL"？** 它们用 Linux 的锁（`worker_lock`），迁到 LKL 后需要 LKL 的 `mutex`，而 LKL 的 `mutex` 需要 LKL 调度器在跑。是否值得迁移，取决于原则 2（EVL 小）和原则 3（改动少）的权衡。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-8]
2. **门的白名单由谁维护？** Dovetail 已有的 oob 安全函数（`irq_work_queue`、`fdget`、`irq_post_inband`……）没有集中的列表，需要从 Dovetail 补丁里整理。
3. **stage 多态函数的分支能否被工具可靠识别？** `running_inband()` 是内联的，分支条件在 objdump 里只是对 `preempt_count` 的位测试，需要源码级分析（如 clang 插件）而不是二进制分析。
4. **"内含 L 知识"的内联代码如何处理？** 方案有三：改写成门调用（有开销）、在 E 域重新实现（改动大）、接受为 TCB 的一部分（削弱隔离）。需要按出现频率和所在路径逐个决定。




我的总结和补充：



只运行在ib域内的evl函数，我们不管他。他想调linux函数就调，把他视作了linux函数。我目前看到的这种函数，只有evl线程进入了ib域，才有可能调用。咱们一方面假设启动过程的ib是安全的，另一方面假设evl线程不会中途在ib和oob之间切换。所以可以不管这些函数。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-9]

其他evl函数有的默认自己可能在ib域内运行也可能在oob域内运行。也有的函数内部会 if(running_inband()){} 出现这种仅ib执行的代码。这种情况可以把判断内部的指令包装成一个函数，从而转化为了只运行在ib域内的函数。

> → 见 [doc 14 §12](14-trust-domain-vs-stage.md) [C12-10]

这样我们就得到了所有需要改写的内部linux函数调用的evl函数。

共享对象的“正确性限制”：oob域内想要调用linux函数，要么是无状态的helper函数，要么就是特别小心的涉及linux共享对象的函数。因为ib会被oob随时打断，所以linux的共享对象的改写很可能尚未完成时oob就已经介入，如果此时oob再调用读写这些linux共享对象的函数，就会有正确性问题。

为此evl函数在不确定自己100%运行在ib域的情况下，只会读写这样的linux共享对象（包括对象或对象中的字段）：
初始化后就不变的，oob读也不担心中断了ib写
单字读写，其实也就是原子读写，也不用担心中断ib的问题
ib的读写已经被改为了关硬中断，而不是软件stall关中断
双副本，oob读写的总是准备好的那个副本

什么叫linux共享对象？就是evl和linux都会读写的对象。

由于共享对象的正确性限制，共享对象本身和相关的操作都比较简单。我们可以按linux对共享对象访问的频率，把共享对象分为三类，不同的处理方法：

ro_after_init的字段：evl可以存一份副本

linux写频率很低的字段：linux每次写都调用evl提供的接口同步修改evl中的副本。evl执行过程中只参考副本的值。evl需要修改的话也是改自己的副本，然后通过irq_work交给ib让linux去修改。

linux频繁写的字段：
