# 16 四个 EVL 子系统：机制解读和可信域划分

> 2026-11-09 第二版。工作区已回退到原始代码（之前的修改存为补丁：`~/xenoami/patches/2026-11-09-shared-state-wip/`），本目录分析的都是**原始代码**。
> 第一版按"oob 读的字段"做闭包；第二版改为**按分配单元的类型划分**（下面的规则 1–10），各篇第 5、6 节已按新规则重写，第 1–4 节（机制解读）不变。
> 输入：工具 `~/xenoami/tools/trust-closure/` 的字段级结果（`out/subsys/`），加上人工读代码。类型级的结论是**人工推导的**，工具还不能直接输出；没有运行验证。

## 每篇的内容

| 文件 | 子系统 | 一句话 |
|---|---|---|
| [sem.md](sem.md) | `sem.c`（68 行） | 内核用的计数信号量。不碰 Linux，但它**总是被嵌在别的结构体里**，所以划分结果取决于容器 |
| [xbuf.md](xbuf.md) | `xbuf.c`（770 行） | EVL 线程和 Linux 线程之间的双向字节流。结构和 proxy 很像，但不调用 `kernel_write/read` |
| [monitor.md](monitor.md) | `monitor.c`（1475 行） | libevl 的互斥锁、条件变量、信号量、标志组在内核里的实现，快速路径在用户态 |
| [observable.md](observable.md) | `observable.c`（1169 行） | 发布 / 订阅通知；EVL 线程的健康监测事件也走这里 |

每篇都分六节：1 功能，2 原理，3 数据结构（逐字段），4 函数语义（逐函数），5 可信域划分（按下面的规则），6 值得注意的地方。

---

## 划分规则

前提（doc 14）：oob 上的执行必须正确；可信域是内存概念，可信代码的结果不能由 Linux 的行为决定。下面的规则回答三个问题：**哪些代码可信**、**哪些数据要可信**、**以什么为单位决定**。

### 一、代码：调用链

**规则 1：oob 函数可信；可信函数的调用者也必须可信，一直追到入口。**
调用者决定了传什么参数、在什么上下文（持什么锁、关不关中断）、什么时候调用。调用者不可信，这些都不可信。调用链在**入口**处结束，入口只有三种：

| 入口 | 调用者 | 参数怎么处理 |
|---|---|---|
| EVL 核心 | 调度器、定时器、oob 中断、oob 系统调用分派 | 可信 |
| 用户态系统调用 | 用户程序 | **用户输入**：只能拷贝 + 校验 |
| Linux 内核调用 EVL | VFS 调 inband 的 fops、`irq_work` 回调、退出钩子 | **不可信**：Linux 传来的指针（`filp`、`work`）不能直接拿来找可信对象 |

> 例（sem）：`evl_down` 在树内的调用者是 `drivers/spi/spidev.c` 的 `run_oob_transfer`，所以 spidev 的这条 oob 路径也是可信代码；`evl_trydown/evl_up` 被 `enable_oob_mode` 调用，它又被 spidev 的 inband ioctl 调用，ioctl 的调用者是 VFS（Linux），所以 ioctl 是一个入口，`arg` 是用户输入。
> 例（xbuf）：`resume_inband_reader(work)` 的调用者是 Linux 的 `irq_work_run`，`work` 是 Linux 传来的指针，`container_of` 算出的 `xbuf` 也就不可信。它只对 Linux 对象调用 `wake_up`，不写可信数据，所以它可以留在不可信侧。monitor 的同名回调还会 `evl_put_element`（写可信的引用计数），那就是"可信代码从 Linux 入口拿到不可信指针"，必须改。

**规则 2：可信代码调用 Linux 函数，只能传不可信对象，返回值只能影响"什么时候"，不能影响可信状态的值。**
在 Linux 等待队列上睡眠是可以的，前提是醒来后在可信锁下重新检查条件：Linux 只能让它晚醒，算不出错误的状态。

> 例（xbuf）：`inbound_wait_input` 在 `ibnd.i_event` 上 `wait_event_interruptible`，醒来后持 hard lock 重查 `fillsz`。可以。
> 反例（proxy，doc 15）：`kernel_write` 的返回值直接写进 `fillsz`。不可以。

### 二、数据：参数、指针、全局

**规则 3：传值参数由调用者负责，被调用者不用追。**
值的正确性在调用者那里已经确定，被调用者拿到的是自己栈上的副本，只有它自己能写。追溯的义务转移到调用者的闭包里。

> 例（sem）：`evl_down_timeout(ksem, timeout)` 的 `timeout` 按值传入，sem 不追，由调用者（spidev）保证。
> 两个例外：
> - **值本身是引用**：指针、句柄（`fundle_t`）、fd。值可信，但它指向的对象要按规则 4 处理。例：monitor 的 `gatefun` 按值传入，要经 `evl_lookup_ns` 查出 monitor。
> - **栈对象的地址被发布出去**：例：monitor 的 `evl_mask_wait` 在等待者栈上，地址存进 `curr->wait_data`，`post_mask` 在**另一个线程**里通过它写回结果。地址逃逸以后，它就不再是"只有自己能写"的副本，按规则 4 处理。

**规则 4：通过指针、全局变量、隐式状态读到的对象，它所在分配单元的类型就是"被依赖的类型"：这个类型的所有写者必须可信，分配和释放也必须可信。**
"隐式状态"指不经参数传入、函数自己去取的东西：`current`、per-CPU 变量、`dovetail_current_state()`。它们最容易漏。

> 例（sem）：`ksem` 是指针，所以要找 `evl_ksem` 类型的所有写者：sem.c 的 5 个函数、sem.h 的初始化和销毁，以及 EVL 等待队列核心（写 `evl_wait_queue`，对所有实例都一样）。
> 例（observable）：`evl_get_subscriber()` 读 `dovetail_current_state()->subscriber`，它在 `task_struct` 里，是 Linux 的分配单元。第一版工具漏掉的就是这一条。

### 三、单位：分配单元

**规则 5：决定的单位是分配单元的类型。** 分配单元 = 一次分配得到的整块内存：`kzalloc(sizeof(T))`、全局或静态的 `T`、一个栈帧。依赖了某个分配单元里的任何一个字段，就依赖整个分配单元，这个类型的**所有实例**都放进可信域。
不区分实例是有意的：区分实例需要精确的指针分析，内核里做不到；按类型划分天然不会因为别名而漏报，代价只是多放一些用不到的实例。

**规则 6：嵌入的成员跟随容器，不单独分类。**
`list_head`、`rb_node`、`hard_spinlock_t`、`evl_wait_queue`、`evl_element` 会被嵌入到很多不同的结构体里，"所有 `list_head` 都可信"没有意义。

> 例（xbuf）：`evl_xbuf` 可信，嵌在里面的两个 `xbuf_ring`、`evl_flag`、`evl_wait_queue` 跟着它可信。

**规则 7：可信类型被嵌入别的结构体时，容器的这个实例必须在可信域里：要么容器整体可信，要么改成指针引用一个单独分配的对象。**

> 例（sem）：`evl_ksem` **从来不是独立的分配单元**，它总是被嵌入。树内唯一的用户 `struct spidev_data`（Linux SPI 驱动，`kzalloc` 分配）里嵌着 `oob.sem`，旁边还有 Linux 的 `struct mutex spi_lock`、`list_head device_entry`。所以"`evl_ksem` 可信"实际的含义是：**`spidev_data` 要么整体进可信域（不可能，Linux 会写它），要么把 `oob` 这个子结构拆出来单独分配。**

**规则 8：可信分配单元里嵌入的、由 Linux 代码写的成员，必须移出去或者换掉。**
这和"不可信代码不写可信数据"直接冲突：加了内存隔离以后，Linux 写它会缺页。

> 例：`wait_queue_head_t`（Linux 的等待代码写）、`struct irq_work`（Linux 的 irq_work 核心写）、`struct rcu_head`（`kfree_rcu` → RCU 写）、`struct cdev`（打开设备时 Linux 改它的引用计数）。
> 处理方式：单独分配在不可信域，可信侧只持指针、只把它交给 Linux 函数、自己不读（对应第一版的 M1）；或者换成 EVL 自己的机制（M2：EVL 自己的 inband 中继）。
> **`evl_element` 本身就有四个这样的成员**（`rcu`、`cdev`、`irq_work`，以及指向 Linux 对象的 `dev`、`fpriv.filp`），所以这一条对**所有** EVL 对象都成立，是 EVL 核心层面的问题。

**规则 9：Linux 的分配单元不移动，依赖它的地方必须截断。**
`struct file`、`task_struct`、`mm_struct` 里 oob 只依赖少数字段，把整个类型移进来等于把 Linux 拉进来。截断方式：镜像、快照、EVL 自己的副本、改成不访问（doc 15 的截断分类）。

> 例：`filp->f_flags`（M3）、`filp->private_data`（M4）、`task_struct` 里的 `subscriber`（M5）。

**规则 10：用户态能写的内存不能可信，里面的值都按用户输入处理。**
可信代码可以持有指向它的指针（可信的地址），可以写它，但读到的值必须校验，或者只当提示。

> 例（monitor）：`__evl_monitor_sstate` 分配在共享堆里，映射给用户态。`evl_mutex.fastlock` 指向其中的 `u.gate.owner`，mutex 核心读到的所有者句柄可能是任意值。

---

## 总表（按分配单元）

| | sem | xbuf | monitor | observable |
|---|---|---|---|---|
| 本子系统的可信分配单元 | **无**（总是被嵌入） | `evl_xbuf` | `evl_monitor` | `evl_observable`、`evl_subscriber`、`evl_observer`（含 backlog 记录） |
| 可以放在不可信域的分配单元 | — | 两块 ring 缓冲区 `bufmem`（**可选**，内容只是透传的数据，见 xbuf.md §5.2） | — | — |
| 用户态可写（规则 10） | — | — | `__evl_monitor_sstate` | — |
| 规则 7：嵌入别人 | `spidev_data.oob` | — | — | — |
| 规则 8：嵌入的 Linux 成员（除 `evl_element` 外） | — | 2 个等待队列头、2 个 irq_work | 2 个等待队列头、2 个 irq_work | 2 个等待队列头、3 个 irq_work |
| 规则 9：依赖的 Linux 分配单元 | — | `struct file`（`f_flags`、`private_data`、`f_op`） | `struct file`（`f_flags`、`private_data`）、`mm_struct` 里的 oob 状态 | `struct file`（`f_flags`、`private_data`）、`task_struct` 里的 `subscriber` |
| 规则 1：Linux 入口拿不可信指针去写可信状态 | — | `xbuf_release`、`factory_dispose`（经 element 释放路径） | `inband_wake_r_irqwork`（`evl_put_element`）、release | 3 个 irq_work 回调、`evl_drop_subscriptions`（退出钩子 + `task_struct` 里的指针） |
| 规则 1：因为调用而变成可信代码的外部代码 | spidev 的 oob 路径和 ioctl | latmus 的测量线程 | `thread.c` 的 `EVL_THRIOC_SIGNAL`、`syscall.c` 的 functl | `thread.c` 的 fops、`evl_notify_thread` 及其调用者（调度器、mutex、wait…） |

## 跨子系统的结论

1. **`evl_element` 要拆分**（规则 8）。它嵌在每一个 EVL 对象里，却带着 `rcu_head`、`cdev`、`irq_work` 和指向 `struct device`、`struct file` 的指针。合理的拆法是：可信部分（引用计数、factory、作用域、命名空间节点）留在对象里；和 Linux 打交道的部分（字符设备、sysfs、RCU 释放、inband 回调）单独分配在不可信域，两边各持对方的句柄。这一条比任何子系统自己的改动都重要，应该先做。
2. **Linux 等待队列和 irq_work 要移出可信对象**（规则 8）。第一版归纳的 M1（oob 偷看等待队列）和 M2（oob 上调 `irq_work_queue`）其实是同一个问题的两个表现：可信对象里嵌着 Linux 写的成员。移出去以后，oob 就没法再偷看（必须改用 EVL 自己的计数），也没法再直接挂 irq_work（必须改用 EVL 自己的中继）。
3. **Linux 入口要有一道门**（规则 1）。inband fops、irq_work 回调、退出钩子都是 Linux 调 EVL，参数里的指针由 Linux 给出。找可信对象不能只靠 `filp->private_data` 或 `container_of(work)`，要在 EVL 自己的表里核对（doc 15 §8.3 的思路：从 `evl_file` 出发）。
4. **调用链会把外部代码拉进可信域**（规则 1）。spidev、latmus、thread.c、调度器都因为调用这些子系统的 oob 函数而成为可信代码。"最小 EVL"的边界因此不只取决于 `kernel/evl/`，还取决于哪些驱动要提供 oob 服务。
5. **per-task 状态放在 `task_struct` 里**（规则 9）。Dovetail 的 `oob_thread_state` 只有三个字段：`thread`（`evl_current()` 的来源，几乎所有 EVL 代码都用）、`subscriber`（observable）、`preempt_count`。observable 的问题只是其中一个实例，三个字段应该一起决定，现实的起点是写成运行时底座的信任假设（doc 13 §11.5）。
6. **共享堆**（规则 10）：`control_mmap` 把整个共享堆映射给调用者，monitor 和线程的 sstate 都在里面。需要确认谁能打开 `/dev/evl/control`，以及 mutex 核心对任意 `fastlock` 值是否健壮。

## 第一版的更正

- **"sem 在树内没有调用者"是错的。** 当时只在 `kernel/evl` 和 `drivers/evl` 里搜索，漏了 `drivers/spi/spidev.c`。这一条更正对结论影响很大：正因为有这个用户，才看出 `evl_ksem` 总是被嵌入（规则 7）。
- 第一版的 M1–M8 仍然成立，但在新规则下的位置是：M1、M2 → 规则 8；M3、M4、M5 → 规则 9；M6（分配器）→ 规则 4 的"分配和释放可信"；M7 → 规则 10；M8 → 规则 1 的 Linux 入口。

## 工具这次暴露的局限

| 局限 | 影响 | 下一步 |
|---|---|---|
| 输出是字段级，不是分配单元级 | 本版的类型级结论全靠人工推导 | 从 AST 收集结构体嵌入关系和分配点（`kzalloc(sizeof(*p))`、全局定义），把可信字段汇总到分配单元，并沿嵌入链向上报告容器（规则 6、7） |
| 只在本子系统的文件里找调用者 | 漏了 spidev 这个调用者 | 对导出函数做全树调用者搜索，输出调用链直到入口（规则 1） |
| EVL 内联函数被当成"核心，固定可信" | 漏了 `task_struct` 里的 `subscriber` | 把 `include/evl/*.h` 的内联函数纳入分析，单独列出"隐式状态"（规则 4） |
| 间接调用要手工解析 | 函数指针、回调、fops | 从结构体初始化和赋值里自动收集"字段 ← 函数"的对应关系 |
| EVL 核心固定为可信 | `evl_element`、等待队列、mutex 内部没有分析 | 下一批分析 `factory.c`（element）、`wait.c`、`mutex.c`、`thread.c` |
