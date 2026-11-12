# proxy.c 的 Linux 依赖：函数、对象、跨域读写

本文盘点 `kernel/evl/proxy.c` 一个文件：它调用了哪些 Linux 函数，直接读写了哪些 Linux 对象，以及这些 Linux 函数反过来读写了哪些 EVL 对象。目的是为"EVL 与 Linux 分属两个域、跨域经过门"的设想提供一份具体的材料。proxy 的完整流程见 [09](09-case-oob-irq-work.md)（irq_work 那一段）和本文第 6 节的路径汇总。

## 0. 方法

- 外部符号：`nm -u build/linux-evl-qemu/kernel/evl/proxy.o`。
- 符号由 proxy.c 中哪个函数引用：`objdump -dr proxy.o`，按函数归并重定位项。
- 内联函数、宏、函数指针调用不会出现在 `nm -u` 里，逐个读源码展开。
- 只统计 proxy.c **自己**的代码（包括展开进来的内联函数）。经由 EVL 函数间接到达的 Linux 函数不展开，只在关键处注明（如 `evl_call_inband_from` → `irq_work_queue`，见 [09](09-case-oob-irq-work.md)）。
- 上下文标记：**oob** / inband / kworker / inband-irq。

## 1. 域的定义（讨论稿）

### 1.1 原始定义

- **Linux 函数**：定义在 EVL 和 Dovetail 以外的函数，主要被 Linux 调用，将来装载到 Linux 地址空间。
- **EVL 函数**：定义在 EVL 中的函数，主要由 EVL 调用，将来装载到 EVL 地址空间。
- **EVL/Linux 对象**：EVL/Linux 函数创建的对象。对象装载到创建者所在的地址空间。
- **跨域访问**：跨域调用和跨域读写都要经过门、受校验；域内访问不校验，以保证低开销。

### 1.2 用 proxy.c 检验这个定义时遇到的问题

| # | 情形 | proxy.c 里的例子 | 按原始定义 | 问题 |
|---|---|---|---|---|
| 1 | Linux 头文件里的内联函数和宏，展开在 EVL 代码里 | `atomic_*`、`raw_copy_from_user`（→ `copy_user_generic`）、`queued_spin_lock`、`get_file`、`wait_event_interruptible` | 定义在 Linux → Linux 函数 | 机器码在 EVL 的目标文件里，执行时也在 EVL 的代码段，不可能"装载到 Linux 地址空间"。它们是**编进 EVL 的 Linux 知识**：把 Linux 结构体的布局和 per-CPU 偏移固化进了 EVL 代码 |
| 2 | EVL 调用 Linux 分配器来分配 EVL 自己的对象 | `proxy`（`kzalloc`）、`bufmem`（`kzalloc`） | 创建者是 `kmalloc` → Linux 对象 | 这两个对象从语义上讲完全属于 EVL：只有 EVL 读写，生命周期由 EVL 控制。"谁创建"应该指**谁决定它的存在和生命周期**，而不是"谁执行了分配" |
| 3 | 嵌在 EVL 对象里的 Linux 类型实例 | `ring->inband_wait_r/w`（`wait_queue_head_t`）、`ring->worker_lock`（`struct mutex`）、`relay_work.irq_work`、`relay_work.wq_work`、`ring->lock` 底层的 qspinlock | 内存在 EVL 对象里；由 Linux 函数初始化（`__init_waitqueue_head`、`__mutex_init`）→ 归属不清 | **这是最难的一类**。实例在 EVL 内存里，但它的协议归 Linux：Linux 函数写它，Linux 的对象挂在它上面（等待项、`raised_list` 的邻居节点、workqueue 链表），它也挂在 Linux 的对象上。双方互相持有指针 |
| 4 | EVL 持有 Linux 对象的指针，并直接访问字段 | 目标文件 `proxy->filp` 的 `f_mode`、`f_pos`、`f_op`；proxy 自己的 `filp->private_data`、`f_flags` | Linux 对象 ✓ | 定义没问题，但 EVL 是**直接读写字段**，没有经过任何函数。`f_pos` 还是 EVL 写的（`proxy.c:110`）。要校验，就只能靠硬件内存保护 |
| 5 | 指向 EVL 内存的指针作为参数传给 Linux 函数 | `kernel_write(filp, ring->bufmem + rdoff, n, &pos)`：读 EVL 缓冲，写 EVL 栈上的 `pos`；`_copy_from_user(&attrs, ...)` 写 EVL 栈 | — | 跨域调用的参数里带着指针，被调用方会顺着指针读写调用方的内存。门不但要校验调用，还要校验（或者拷贝）指针所指的数据 |
| 6 | EVL 函数在 Linux 分配的栈上执行 | inband 时 `proxy_write`、`relay_output` 运行在 Linux 任务（或 kworker）的内核栈上；`wait_event_interruptible` 把等待项放在这个栈上，并挂进 EVL 对象 `inband_wait_w` | 栈由 Linux 创建 → Linux 对象 | EVL 函数的局部变量全都在 Linux 的内存里。oob 线程的栈也是 Linux 创建任务时分配的 |
| 7 | EVL 的 per-CPU 对象放在 Linux 的 per-CPU 区 | `evl_runqueues`（`include/evl/sched.h:106`），proxy.c 里经由内联的 `evl_schedule` → `this_evl_rq()` 访问，引用 `this_cpu_off` | 由 EVL 定义 → EVL 对象 | 内存位于 Linux 管理的 per-CPU 区域，寻址要用 Linux 的 per-CPU 基址 `this_cpu_off` |
| 8 | Linux 对象里存着指向 EVL 对象的指针 | `current->thread_info.oob_state.thread` 指向 `evl_thread`（`evl_current()`，`include/evl/thread.h:228`）；`filp->f_oob_state.data` 指向 `evl_file`；`filp->private_data` 指向 EVL 的 binding | — | EVL 通过 Linux 对象来找到自己的对象，而且是在 oob 热路径上 |
| 9 | Dovetail | `hard_local_irq_save`、`running_inband()`、`run_oob_call`、`irq_post_inband` | 两者都不是 | 它是两个域之间的仲裁层，天然就是"门"所在的位置。它要读写的状态（stage、stall 位、中断日志）两边都会碰 |
| 10 | 用户内存 | `do_proxy_write` 读 `u_buf`，`do_proxy_read` 写 `u_buf` | 两者都不是 | 第三个域。现在由 `raw_copy_*_user` 直接访问（EVL 的 oob 路径上似乎没有 `access_ok` 检查，见[第 5 节](#5-附带发现)） |

### 1.3 修订建议

**函数的域**：按**机器码放在哪里**来划分，而不是按源码定义在哪里。

- EVL 目标文件里的所有代码都是 EVL 函数，包括从 Linux 头文件展开进来的内联函数和宏。
- 但要单独标记那些"**内含 Linux 知识**"的内联代码：它们按 Linux 的布局访问 Linux 对象（问题 1），是不经过门的跨域访问。

**对象的域**：按"**谁控制它的生命周期，它的内存位于哪个域**"划分，分成四类：

| 类别 | 定义 | proxy.c 的例子 | 访问规则 |
|---|---|---|---|
| EVL 私有 | EVL 决定生灭，只有 EVL 读写 | `proxy` 结构体本身、`bufmem`、`fillsz`/`rdoff`/`wroff` 等字段、`oob_wait` | 域内访问，不校验。要求从 **EVL 自己的分配器**分配（解决问题 2） |
| Linux 私有 | Linux 决定生灭，只有 Linux 读写 | workqueue 和它的 pool、kworker、slab 元数据 | EVL 只持有不透明句柄（如 `ring->wq`），只能经由门调用 |
| **共享对象** | 两个域都要按协议读写 | 问题 3 的嵌入实例；问题 4 的 `struct file` 字段；问题 5 的数据缓冲 | 放在**共享区**，或者改成句柄加门访问，或者在门上拷贝 |
| 运行时基础 | 执行本身依赖的状态 | 栈、per-CPU 区、`current`、Dovetail 的 stage 状态 | 需要单独设计（问题 6、7、8、9） |

**和 LKL 的关系**：在这个模型里，LKL 是 **EVL 域的私有库**。EVL 调 LKL 是域内调用，不校验。前面的 helper 迁移，本质上就是把"EVL → Linux 的跨域调用"变成"EVL → LKL 的域内调用"。

**一个硬约束**：跨域**调用**可以靠门函数来拦截，跨域**读写**不行。普通的 `mov` 指令没法用软件拦截，只能靠硬件内存保护：两套页表，或者 x86 的保护键（PKS）。这意味着：

- 问题 1、4、7、8 里那些内联的字段访问，在隔离之后都会触发保护错误，必须事先改写成门调用。
- 它们当中有一部分在 oob 热路径上，比如 `current`、`preempt_count`、per-CPU 基址。改写后每次访问都要付出门的开销。

## 2. proxy.c 调用的 Linux 函数

形式一栏：

- **外**：外部符号，出现在 `nm -u` 里；
- **内**：内联函数；
- **宏**：宏；
- **间**：通过函数指针间接调用。

### 2.1 oob 路径

| Linux 函数 | 形式 | proxy.c 调用者 | 读写的 Linux 对象 | 读写的 EVL 对象 |
|---|---|---|---|---|
| `queued_spin_lock` | 内 | `do_proxy_write`、`do_proxy_read`（经由 `raw_spin_lock_irqsave` → `hard_spin_lock_irqsave`）；`proxy_oob_write`、`proxy_oob_read`（经由 `evl_wait_flag` 内联的 `wchan.lock`） | — | 写 `ring->lock`、`oob_wait.wait.wchan.lock` |
| `queued_spin_lock_slowpath` | 外 | 同上，只在锁竞争时调用 | 读写 **Linux 全局的 per-CPU MCS 节点**（`qnodes`） | 写同上两把锁 |
| `queued_spin_unlock` | 内 | 同上 | — | 写同上 |
| `atomic_read` / `atomic_add_return` / `atomic_sub` / `atomic_add` / `atomic_cmpxchg` | 内 | `do_proxy_*`、`proxy_oob_read`、`proxy_oob_poll` | — | 读写 `fillsz`、`reqsz`、`on_eof` |
| `raw_copy_from_user` → `copy_user_generic` | 内 | `do_proxy_write` | 读**用户内存** `u_buf` | 写 `bufmem` |
| `raw_copy_to_user` → `copy_user_generic` | 内 | `do_proxy_read` | 写**用户内存** `u_buf` | 读 `bufmem` |
| `rep_movs_alternative` | 外 | 上两者的回退实现。CPU 支持 FSRM 时，启动后被替换成 `rep movsb`，不会调用 | 同上 | 同上 |
| `memset` | 外 | `do_proxy_write`：拷贝失败时清零 | — | 写 `bufmem` |
| `min` / `rounddown` / `container_of` | 宏 | 多处 | — | — |
| `f_op->oob_poll` | 间 | `proxy_oob_poll`：调用目标文件的 oob poll（如果有） | 由目标驱动决定 | 传入 `filp` 和 `wait`（见[第 5 节](#5-附带发现)的可疑点） |

oob 路径上，proxy.c **自己的代码**直接访问的 Linux 对象：

| Linux 对象 | 字段 | 读/写 | 位置 |
|---|---|---|---|
| proxy 自己的 `struct file` | `private_data` | 读 | `element_of()`，每个 `fops` 入口都有 |
| | `f_flags`（`O_NONBLOCK`） | 读 | `proxy_oob_write`、`proxy_oob_read`、`do_proxy_read` |
| 目标文件 `proxy->filp` | `f_op`、`f_op->oob_poll` | 读 | `proxy_oob_poll` |
| `pcpu_hot` | `current_task`、`preempt_count` | 读 | `evl_current()`、`running_inband()`（`stage_level()` 读 `preempt_count` 里的 stage 位） |
| `current->thread_info.oob_state` | `thread` | 读 | `evl_current()`，在 `evl_wait_flag` 等内联函数里 |
| per-CPU 基址 | `this_cpu_off` | 读 | `this_evl_rq()`，用来找 EVL 的 per-CPU 运行队列 |

经由 EVL 函数间接到达的 oob Linux 依赖：

- `evl_call_inband_from` → `irq_work_queue`，写 Linux 的 per-CPU `raised_list`，见 [09](09-case-oob-irq-work.md)。
- EVL 的 oob `write`/`read` 系统调用入口 → `evl_get_file` → `fdget`，读当前进程的 fd 表（`kernel/evl/file.c:34`）。

### 2.2 inband 读写路径

| Linux 函数 | 形式 | proxy.c 调用者 | 上下文 | 读写的 Linux 对象 | 读写的 EVL 对象 |
|---|---|---|---|---|---|
| `mutex_lock` / `mutex_unlock` | 外 | `relay_output`、`relay_input` | kworker 或 inband | 写目标文件的 `f_pos_lock`；竞争时读写 `current` 和调度器状态 | 写 `ring->worker_lock` |
| **`kernel_write`** | 外 | `relay_output` | 同上 | 目标文件、inode、超级块冻结计数、LSM、fsnotify、目标驱动或文件系统的全部状态、`current->ioac`（详见 [§2.5](#25-kernel_write--kernel_read-内部)） | 读 `bufmem`；写栈上的 `pos` |
| **`kernel_read`** | 外 | `relay_input` | 同上 | 同上（读方向） | 写 `bufmem`；写栈上的 `pos` |
| `__wake_up` | 外 | `relay_output`、`relay_input`（`wake_up` 宏） | 同上 | 读写等待者的任务状态和 Linux 运行队列 | 读写 `inband_wait_w` / `inband_wait_r` 的链表 |
| `init_wait_entry` / `prepare_to_wait_event` / `schedule` / `finish_wait` | 外 | `proxy_write`（`wait_event_interruptible` 宏展开） | inband | 等待项放在 **Linux 任务的栈上**；`current` 的状态；调度器 | 把栈上的等待项挂进、摘出 `inband_wait_w` |
| `__SCT__might_resched` | 外 | 同上 | inband | 调度器（`might_resched`） | — |
| `poll_wait` → `p->_qproc` | 内 + 间 | `proxy_poll` | inband | Linux 的 poll table，以及 poll 调用者的等待项 | `_qproc` 把等待项挂进 `inband_wait_w` / `_r` |
| `f_op->poll` | 间 | `proxy_poll` | inband | 目标驱动 | — |
| `get_file` | 内 | `proxy_mmap` | inband | 写目标文件的 `f_count` | — |
| `__warn_printk` | 外 | `proxy_mmap`（`get_file` 里的 `WARN_ONCE`） | inband | printk | — |
| `call_mmap` → `f_op->mmap` | 内 + 间 | `proxy_mmap` | inband | 目标驱动、`vma` | — |
| `fput` | 外 | `proxy_mmap` | inband | 写 `f_count`，可能触发文件释放 | — |

inband 路径上，proxy.c 自己的代码直接访问的 Linux 对象：

| Linux 对象 | 字段 | 读/写 | 位置 |
|---|---|---|---|
| 目标文件 | `f_mode`（`FMODE_ATOMIC_POS`） | 读 | `relay_output:89`、`relay_input` |
| | **`f_pos`** | **读、写** | `relay_output:92/110`、`relay_input` |
| | `f_pos_lock` | 写（加锁） | 同上 |
| | `f_op->poll`、`f_op->mmap` | 读 | `proxy_poll`、`proxy_mmap` |
| `vm_area_struct` | **`vm_file`** | **写** | `proxy_mmap:578` |

### 2.3 创建和销毁（inband）

| Linux 函数 | 形式 | proxy.c 调用者 | 读写的 Linux 对象 | 读写的 EVL 对象 |
|---|---|---|---|---|
| `_copy_from_user` | 外 | `proxy_factory_build`（`copy_from_user`，带 `access_ok`） | 读用户内存 | 写栈上的 `attrs` |
| `fget` | 外 | `proxy_factory_build` | 读 fd 表；写目标文件的 `f_count` | 返回的指针存进 `proxy->filp` |
| `fput` | 外 | `proxy_factory_build`（失败回滚）、`proxy_factory_dispose` | 写 `f_count`，可能触发文件释放 | 读 `proxy->filp` |
| `__kmalloc_cache_noprof` + `kmalloc_caches` | 外 | `proxy_factory_build`（`kzalloc(sizeof(*proxy))`） | 读写 slab 状态 | **创建** `proxy` 对象（[问题 2](#12-用-proxyc-检验这个定义时遇到的问题)） |
| `__kmalloc_noprof` | 外 | `init_ring`（`kzalloc(bufsz)`） | 同上 | **创建** `bufmem` |
| `alloc_workqueue` | 外 | `init_ring`（`alloc_ordered_workqueue` 宏） | **创建** workqueue，以及背后的 pool 和 kworker | 读元素名（格式化成 workqueue 名字） |
| `raw_spin_lock_init` | 宏 | `init_ring` | — | 写 `ring->lock` |
| `__init_waitqueue_head` | 外 | `init_ring`（`init_waitqueue_head` 宏） | lockdep 类（如果打开） | 写 `inband_wait_r` / `_w` |
| `__mutex_init` | 外 | `init_ring`（`mutex_init` 宏） | 同上 | 写 `worker_lock` |
| `kfree` | 外 | `init_ring`、`proxy_factory_build`、`destroy_ring` | slab 状态 | **释放** `bufmem`、`proxy`；slab 会把空闲链表指针写进被释放的对象里 |
| `irq_work_sync` | 外 | `destroy_ring`（内联进 build/dispose；经由 `evl_cancel_work`，`work.h:83`） | — | 读 `relay_work.irq_work` 的 BUSY 位并等待 |
| `cancel_work_sync` | 外 | 同上 | workqueue 的 pool 锁和链表 | 读写 `relay_work.wq_work` |
| `destroy_workqueue` | 外 | `destroy_ring` | **销毁** workqueue | 读 `ring->wq` |
| `kvfree_call_rcu` | 外 | `proxy_factory_dispose`（`kfree_rcu` 宏） | 挂进 Linux 的 per-CPU RCU 回调链表；宽限期后由 **Linux 释放 EVL 对象** | 写 `proxy->element.rcu` |

### 2.4 Dovetail 函数（单列，不算 Linux）

| 函数 | 形式 | proxy.c 调用者 | 作用 |
|---|---|---|---|
| `hard_local_irq_save` / `hard_local_irq_restore` | 内 | 所有 hard 锁的加锁、放锁 | 关闭、恢复 CPU 的真实中断 |
| `running_inband()` | 内 | `do_proxy_write` | 读 `preempt_count` 里的 stage 位 |
| `run_oob_call` | 外 | `do_proxy_read`、`relay_*`、`proxy_release`（经由内联的 `evl_schedule`，在 inband 上调用时） | 临时切到 oob stage 执行 `__evl_schedule` |
| `irq_post_inband` | — | 经由 `evl_call_inband_from` → `irq_work_queue` → `irq_local_work_raise` | 在 inband 中断日志里置位 |
| `oob_write` / `oob_read` / `oob_poll`（`file_operations` 的新成员） | — | `proxy_fops` | Dovetail 给 `file_operations` 加的 oob 入口 |

### 2.5 `kernel_write` / `kernel_read` 内部

`fs/read_write.c:647` / `:538`：

| 步骤 | 碰到的 Linux 状态 |
|---|---|
| `rw_verify_area` | 文件锁、LSM（`security_file_permission`） |
| `file_start_write` → `sb_start_write`（只针对普通文件） | 超级块的冻结计数（per-CPU 读写信号量） |
| `f_mode`、`f_op` 检查 | 读 `struct file` |
| **`f_op->write_iter` / `read_iter`** | **目标驱动或文件系统**：tty、ext4 + 页缓存 + 块设备、pipe、socket…… |
| `fsnotify_modify` / `fsnotify_access` | inotify / fanotify 监听者 |
| `add_wchar` / `add_rchar`、`inc_syscw` / `inc_syscr` | `current->ioac`（kworker 的 I/O 统计） |

`struct file` 本身几乎只是被读了几个字段，它是一个入口，通过 `f_op` 把调用分派到宿主的任意子系统。

## 3. 反过来：Linux 函数读写了哪些 EVL 对象

把第 2 节按 EVL 对象重新整理：

| EVL 对象 | 被哪些 Linux 函数读写 | 性质 |
|---|---|---|
| `proxy` 结构体整体 | `__kmalloc_cache_noprof` 创建；`kfree` / `kvfree_call_rcu` 释放 | 生命周期的起止由 Linux 分配器完成 |
| `ring->bufmem` | `__kmalloc_noprof` 创建；`kernel_write` 读；`kernel_read` 写；`rep_movs_alternative`、`memset` 读写；`kfree` 释放 | **数据通道**：跨域传递的数据本身 |
| `ring->lock`、`oob_wait.wait.wchan.lock` | `queued_spin_lock_slowpath` 写 | 嵌入的 Linux 锁原语；slowpath 还会把这把锁和 Linux 的 per-CPU MCS 节点串在一起 |
| `ring->worker_lock` | `__mutex_init`、`mutex_lock`、`mutex_unlock` | 嵌入的 Linux mutex。竞争时，Linux 任务的等待项会挂在它上面 |
| `ring->inband_wait_r` / `_w` | `__init_waitqueue_head`、`prepare_to_wait_event`、`finish_wait`、`__wake_up`、`poll_wait` 的 `_qproc` | 嵌入的 Linux 等待队列，**Linux 任务栈上的等待项挂进 EVL 对象** |
| `ring->relay_work.irq_work` | `irq_work_queue`（经由 EVL）、`irq_work_run`、`irq_work_sync` | 嵌入；入队期间和 Linux 的其他 irq_work 串在 `raised_list` 上（[09](09-case-oob-irq-work.md)） |
| `ring->relay_work.wq_work` | `queue_work`（经由 EVL）、`process_one_work`、`cancel_work_sync` | 嵌入；入队期间挂在 workqueue 的链表上 |
| `proxy->element.rcu` | `kvfree_call_rcu` | 嵌入的 `rcu_head`，挂在 Linux 的 RCU 回调链表上 |
| 元素名（`evl_element_name`） | `alloc_workqueue` 读 | 作为字符串参数 |
| 栈上的 `pos`、`attrs` | `kernel_write`/`kernel_read` 写 `pos`；`_copy_from_user` 写 `attrs` | 指针参数 |

## 4. 反方向调用：Linux 调用 EVL

跨域门要双向：Linux 也会通过函数指针调用 EVL 的函数。

| Linux 一方 | 调用的 EVL 函数 | 上下文 |
|---|---|---|
| VFS（`open`、`close`、`vfs_write`、`vfs_read`、`poll`、`mmap`） | `proxy_fops` 里的 `evl_open_element`、`proxy_release`、`proxy_write`、`proxy_read`、`proxy_poll`、`proxy_mmap` | inband |
| `irq_work_run`（Dovetail 回放 sirq 时调用） | `__evl_do_irq_work` | inband-irq |
| workqueue 的 `process_one_work` | `__evl_do_work` → `relay_output_work` / `relay_input_work` | kworker |
| `run_oob_call`（Dovetail） | `__evl_schedule` | 临时 oob |
| EVL 自己的 oob 系统调用入口 | `proxy_oob_write`、`proxy_oob_read`、`proxy_oob_poll` | **oob**（EVL → EVL，不跨域） |

## 5. 附带发现

- `proxy_oob_poll:488` 调用目标文件的 `oob_poll` 时，传的是 proxy 自己的 `filp`；inband 版本 `proxy_poll:566` 传的是 `proxy->filp`。**2026-11-04 更新**：读代码确认是 bug。能走到这一步的目标实际上只有 socket（打开 `CONFIG_EVL_NET` 后所有 socket 都带 `sock_oob_poll`；EVL 设备没有 `read_iter`，proxy 本来就转发不了），这时 `sock_oob_poll` 会把 proxy 的 `evl_file_binding` 当成 `evl_socket`，调用乱指的函数指针。而且这一步转发在语义上就不成立（proxy 的读写从不走目标的 oob 路径），已在 [impl-shared-state/01](impl-shared-state/01-drop-oob-poll-forward.md) 中删除。尚未运行复现。
- EVL 的 oob 读写路径上没有 `access_ok`（`kernel/evl` 里 grep 不到），`raw_copy_*_user` 可能接受内核地址。未验证。
- `relay_output` 出错退出时，如果 ring 里还剩新数据，`fillsz` 不会回到 0，之后的写入都不会再触发 relay，proxy 可能卡死。读代码推断，未实测。

## 6. 按"门"模型的观察

### 6.1 每条路径的跨域次数

| 路径 | 跨域调用（EVL → Linux） | 内联的跨域读写（没法用门拦截） |
|---|---|---|
| **oob 写**（`proxy_oob_write` → `do_proxy_write`） | 竞争时 `queued_spin_lock_slowpath`；`irq_work_queue`（只在空变非空时）；`memset`（只在出错时）；系统调用入口的 `fdget` | `filp->private_data`、`f_flags`；`current`、`preempt_count`、`this_cpu_off`；用户内存 |
| **oob 读** | 同上，再加 `run_oob_call`（Dovetail） | 同上 |
| kworker 写文件（`relay_output`） | `mutex_lock`×2、`kernel_write`×n、`mutex_unlock`×2、`__wake_up` | `f_mode`、`f_pos`（读写） |
| 创建和销毁 | 约 15 个 | — |

oob 路径上的跨域调用很少，而且大部分是条件触发的。真正的麻烦在右栏：**oob 热路径上那些内联的字段访问**。在硬件隔离下，每一项都要改造成门调用，或者把数据挪到 EVL 域。

### 6.2 最难处理的三类

1. **嵌入的 Linux 原语**（`wait_queue_head_t`、`struct mutex`、`irq_work`、`work_struct`、`rcu_head`、qspinlock）：
   - 改法一：EVL 只保留句柄，实例放到 Linux 一侧。代价是每次使用都要多一次门调用，还要额外管理生命周期。
   - 改法二：整个原语换成 EVL 自己的实现。`irq_work` 就是方案 (a)（[09](09-case-oob-irq-work.md)）。
   - 改法三：把这些实例放进共享区。
2. **`struct file` 的字段访问**：`f_pos` 的读写、`f_pos_lock` 都应该收进一个门函数，比如"按 proxy 的语义写这个文件"。这样也顺便把 [05](05-dependency-inventory.md) 里的锁嵌套问题（`worker_lock` 套 `f_pos_lock`）封装到了门的另一侧。
3. **运行时基础**：`current`、`preempt_count`、per-CPU 基址、栈。这些不是 proxy 特有的问题，而是整个 EVL 都会遇到的，应该在 Dovetail 层统一解决，比如由 Dovetail 提供 EVL 域里可以直接读的镜像。

### 6.3 proxy 在这个模型里的定位

proxy 的 inband 部分（`relay_*`、VFS 入口）就是 EVL 和 Linux 之间的**桥**：它的职责就是跨域，跨域调用集中在这里是合理的。把这一段整体放到门的 Linux 一侧，相当于让门的接口变成"把这段缓冲按 proxy 的语义写进这个文件"，粒度粗，而且只在 inband 上发生，不影响实时性。门的设计应该集中精力放在 oob 那一侧。
