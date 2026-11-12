# 对象分类

EVL 依赖的 Linux 对象按"能否复制、归谁管理"分三类。

## 第一类：可以复制两份

EVL 用自己的一份（由 LKL 提供），与宿主 Linux 完全无关。EVL 原来调用的 Linux 函数改为调用 LKL 的函数，操作的也是 EVL/LKL 自己的对象。

例子：
- 纯数据结构和算法（rbtree、位图、字符串）
- EVL 私有的锁和数据，比如 factory 的名字哈希表及其 `hash_lock`（`factory.c:470-480`）
- EVL 私有的内存对象（element 结构体等）

## 第二类：不能复制，交给 EVL 管理

Linux 只能通过 EVL 提供的接口读写这些对象。
- 先例：Dovetail 已经让 EVL（oob）以最高优先级掌管中断和定时器硬件，Linux 看到的是虚拟化的中断状态（stall 位）。

## 第三类：不能复制，也不能完全交给 EVL

不能交给 EVL 的原因主要有三个：
1. **规模**：交给 EVL 会让 EVL 膨胀，违反原则 2。例如进程地址空间（mm/VMA/缺页）、信号、ptrace 这些 POSIX 进程语义。
2. **语义归 Linux**：对象的含义和生命周期由 Linux 定义，Linux 访问又很频繁，每次绕道 EVL 接口改动量无法接受。
3. **锁的方向反了（最致命）**：对象由 EVL 管，Linux 修改它时就要拿 EVL 的锁；Linux 可能长时间持锁，RT 线程被非实时路径阻塞，违反原则 1。

典型例子：
- **task 本身**：EVL 线程就是一个 Linux task（inband/oob 双重身份，Dovetail 切换），共用 task_struct、mm、fd 表。EVL 里 `current` 出现约 90 次；Dovetail 的 oob 线程状态嵌在 `thread_info`（`dovetail_current_state()`），还有嵌在 `mm_struct` 里的 `dovetail_mm_state()`。
- **用户地址空间**：页表归 Linux 管，oob 上 `raw_copy_from_user` 等直接访问。
- **时间**：见 [08-open-questions.md](08-open-questions.md) 中关于 `ktime_get_mono_fast_ns` 的讨论。
- 同时有 inband 和 oob 路径的设备驱动（oob 网卡、SPI、GPIO）。

## 处理第三类对象的思路

- **分区**：把对象切开转成第一类（物理内存分一段给 EVL、隔离 CPU、设备直接分配）。
- **按 stage 交接所有权**：谁在运行谁拥有。EVL 现在的做法就是这样：实时路径碰不得的东西，就把线程降回 inband 去做。
- **单向访问协议**：RT 一侧只读或用无锁/RCU/latch 协议；写操作异步交给非实时一侧（类似 `evl_call_inband`）。RT 路径永远不同步等待对方。

## 讨论中出现的新类别

**有状态但可以无锁读取**：写方需要进入 LKL（或 Linux）上下文，读方不需要。例如用 seqcount latch 保护的时间数据，oob 读者可以直接按 latch 协议读。原有三分类里没有这一类。

## 例子：irq_work 应该归哪类

见 [09-case-oob-irq-work.md](09-case-oob-irq-work.md) 和 [08-open-questions.md](08-open-questions.md)。结论倾向：通知机制本身（向 inband 注入中断）属于 Dovetail，不能复制；队列可以复制一份，归第一类。
