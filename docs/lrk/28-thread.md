# 28 thread.c 的 Z/z 改写

本文可以单独阅读：说明 `kernel/evl/thread.c` 是干什么的、它和 Linux 有哪些交集、这次改了什么、为什么其余的不改。方法沿用 doc 26：先按流程列出谁调用、谁读写，再代入 Z/z 找交集，最后截断。

代码位置写法：`orig:` 指原版只读副本 `~/xenoami/linux-evl-orig`，`now:` 指当前的 `~/xenoami/linux-evl`。

---

## 0. 结论先行

- **thread.c 大部分是纯 Z 代码。** 睡眠和唤醒、hold 和 release、超时、周期、调度参数、取消、kick、demote，都只操作 EVL 的线程、运行队列和定时器。
- **出向（Z → z）三处在 doc 24 里已经改完**：P1 线程切回 inband、P2 内核线程启动、P3 发信号，都改成了通知中继（doc 27）。
- **剩下的交集几乎都是入向（z → Z）**，分四类：

  | 类 | 内容 | 处理 |
  |---|---|---|
  | A | **EVL 线程本身就是 Linux 任务**带来的耦合：Linux 给它发信号、迁移它、ptrace 它、让它退出，EVL 都要跟着动（Dovetail 事件回调） | 不截断。这是 EVL 的设计前提，§5.1 写明边界 |
  | B | **z 发起的控制请求**：inband ioctl 改调度参数、demote、挂起；`evl_killall`；sysfs 读状态 | 大多已经是"传值调用 Z 函数"的门的形状。**sysfs 不干净，这次改了**；"z 能不能控制实时线程"是策略问题，需要你决定（§5.2） |
  | C | **Z 读 z 的值**：`task_cpu()`、`task_pid_nr()`、`mm->def_flags`、`current->ptrace` | `task_cpu()` 被拿来当下标，**这次加了约束**；其余只用于显示，或只影响线程自己 |
  | D | **嵌在 Z 对象里的 Linux 对象**：`exited` completion、`altsched`、element 的 `cdev` 等 | 不在这次范围，归 doc 24 N2，和拆 element 一起做 |

- **这次改了三处**（§4）：
  1. sysfs 的 7 个读接口改成"Z 门返回值、z 自己格式化"：z 不再持有 Z 的锁去执行格式化，Z 的调度类回调也不再往 z 的缓冲区里写；
  2. `pin_to_initial_cpu` 对 z 给的 CPU 号做范围约束；
  3. `evl_killall` 把 printk 移出 `thread_list_lock`，这把锁 oob 上也会拿。
- **顺带发现两个原版 bug**（§6），没有改：`EVL_THRIOC_GET_STATE` 返回的 `state` 字段其实是 CPU 号；attach 失败时线程名没有释放。
- **验证**：回归测试 74/78，与基线相同；sysfs 并发读取 4 万多次，没有内核告警（§7）。

---

## 1. thread.c 是干什么的

EVL 线程就是一个 Linux 任务（`task_struct`）配上一个 `struct evl_thread`，由两个调度器共同管理（doc 26 §3）。Dovetail 在 `task_struct` 里给 EVL 留了一块状态（`oob_thread_state`），里面存着指向 `evl_thread` 的指针；`evl_current()` 和 `evl_thread_from_task(p)` 都是通过它找到 EVL 线程的。

thread.c（`now:` 2900 多行）按功能分组：

| 组 | 函数 | 在哪运行 | 谁发起 |
|---|---|---|---|
| 创建和映射 | `evl_init_thread`、`map_uthread_self`、`thread_factory_build`、`map_kthread_self`、`kthread_trampoline`、`__evl_run_kthread` | inband | 用户线程 attach（clone ioctl），或驱动创建内核线程 |
| 睡眠和唤醒 | `evl_sleep_on`、`evl_wakeup_thread`、`evl_hold_thread`、`evl_release_thread`、`evl_unblock_thread` | 任意，主要在 oob | EVL 自己 |
| 时间 | `evl_delay`、`evl_sleep_until`、`evl_set_period`、`evl_wait_period`、`evl_get_thread_timeout` | 主要在 oob | EVL 线程 |
| 调度参数 | `evl_set_thread_schedparam`、`set_sched_attrs`、`get_sched_attrs`、`evl_get_thread_state`、`update_mode` | 两边都有 | ioctl |
| 取消、kick、demote | `evl_cancel_thread`、`evl_kick_thread`、`evl_demote_thread`、`evl_join_thread`、`evl_killall` | 两边都有 | EVL 自己，或 ioctl |
| 信号、健康监视 | `evl_signal_thread`、`evl_notify_thread` | 任意 | EVL 自己（P3） |
| Dovetail 事件 | `handle_oob_trap_entry/exit`、`handle_oob_mayday`、`handle_inband_event` → `handle_{sigwake,migration,ptstop,ptcont,retuser,cleanup}_event` | trap 在 oob，其余在 inband | **Linux** |
| 清理 | `cleanup_current_thread`、`do_cleanup_current`、`uninit_thread`、`thread_factory_dispose` | inband | 线程退出、exec、detach |
| 文件接口 | `thread_ioctl`、`thread_oob_ioctl`、`thread_*read/write/poll` | 两边 | 用户进程 |
| sysfs | `state_show`、`sched_show`、`stats_show`、`timeout_show`、`pid_show`、`observable_show`、`wchan_show` | inband | 任何读 `/sys/devices/virtual/thread/<名字>/` 的进程 |

---

## 2. 按发起者看：哪些是 z → Z 的入口

doc 26 的规则是"Z 不依赖 z"。z 能调用 Z，但只能通过**门**：只传值，在 Z 自己的代码里拿锁、检查、修改；Z 持锁期间不碰 z 的内存，也不执行 z 的代码。

thread.c 里由 z 发起、进入 Z 的入口：

| 入口 | 发起者 | 传进来什么 | Z 做什么 |
|---|---|---|---|
| E1 Dovetail 事件（`handle_inband_event`） | Linux 内核：发信号（`kernel/signal.c:766`、`993`）、迁移（`kernel/sched/core.c:3093`）、ptrace 停止和继续、任务退出、mm 释放、返回用户态 | `task_struct *` 或 `current` | kick、解冻、切 oob、拆除线程 |
| E2 inband ioctl（`thread_ioctl`） | 用户进程，经 Linux VFS | `file`（经 `private_data` 找到线程）、命令号、用户参数 | 改调度参数、改模式位、unblock、demote、挂起和恢复、join、detach |
| E3 `evl_killall` | control 设备的 ioctl | 掩码 | 取消所有匹配的线程 |
| E4 sysfs 读 | 任何进程 | `struct device`（经 `drvdata` 找到线程）、输出缓冲区 | 读状态 |
| E5 attach（`thread_factory_build`） | 用户线程自己 | 名字、clone 标志 | 创建并映射线程 |

另外还有 Z 读 z 数据的地方，不是入口，但也是交集：`task_cpu(p)`、`task_pid_nr(task)`、`mm->def_flags`、`current->ptrace`、`signal_pending(current)`。

---

## 3. 交集清单

| # | 交集 | 位置（`now:`） | 方向 | 影响 | 处理 |
|---|---|---|---|---|---|
| T1 | sysfs 读接口持有 Z 的硬锁执行 z 的格式化；调度类回调 `sched_show` 持锁往 z 的缓冲区里写 | `sched_show`、`stats_show`、`wchan_show` | z → Z | z 想读多频繁、读多久都由它决定，读的时候还关着硬中断；Z 代码按 z 给的指针写内存 | **已截断**：门（§4.1） |
| T2 | 读 `stats` 会改 Z 的 `stat.lastperiod` | `stats_show` | z → Z | z 写了 Z 的字段 | **已整理**：改由门里的 Z 代码写；这个字段只服务于这个门，Z 的决策不依赖它（§4.1） |
| T3 | `task_cpu(p)` 被当作下标，去索引 Z 的 per-CPU 运行队列 | `pin_to_initial_cpu` | z → Z | 值越界时，Z 会拿一块任意内存当运行队列 | **已截断**：输入化，先约束范围（§4.2） |
| T4 | `evl_killall` 持 `thread_list_lock` 调用 printk（只在 debug 时） | `evl_killall` | Z 持锁调 z | oob 上的 `enqueue_new_thread` 也要拿这把锁 | **已改**：移出锁（§4.3） |
| T5 | Dovetail 事件：Linux 发信号、迁移、ptrace、退出时，EVL 线程会被 kick 出 oob、冻结、拆除 | `handle_*_event` | z → Z | z 能让一个实时线程离开 oob | **不截断**：EVL 线程就是 Linux 任务（§5.1） |
| T6 | 从 `task_struct` 找 EVL 线程，用的是 Dovetail 存在 `task_struct` 里的指针 | `evl_thread_from_task`、`evl_current` | z → Z | 指针存放在 z 的内存里 | 与 doc 24 的 Y8 相同，归入信任基（doc 23）；物理隔离时要换成 Z 自己的映射表，同 N1 |
| T7 | inband ioctl 能改实时线程的调度参数、demote、挂起；`evl_killall` 能取消所有线程 | `thread_common_ioctl`、`__thread_ioctl`、`evl_killall` | z → Z | 合法 API，但效果是 z 控制了 Z 的调度 | **待决定**：策略问题（§5.2） |
| T8 | 经 `file->private_data` 或 `dev->drvdata` 找到线程 | `element_of(filp)`、`evl_get_element_by_dev` | z → Z | 和 Y7 相同 | 句柄表（doc 24 N1） |
| T9 | `task_pid_nr()`、`thread->name` | `evl_get_inband_pid`、`note_trap`、HM 打印 | Z 读 z | 只用于显示 | 记录，不截断 |
| T10 | `mm->def_flags & VM_LOCKED`（要求先 `mlockall`） | `map_uthread_self` | Z 读 z | 只影响这个线程自己：z 撒谎，线程以后可能缺页 | 记录，不截断（doc 24 §1 的"只影响自己"） |
| T11 | `current->ptrace`、`signal_pending(current)` | trap 处理、`evl_switch_oob` | Z 读 z | 只影响当前线程 | 同 doc 24 的 Y10 |
| T12 | `dovetail_send_mayday(p)`、`dovetail_request_ucall(p)` 写 Linux 任务的标志位 | `evl_kick_thread` 等 | Z 写 z | Dovetail 原语 | 归入信任基（doc 27 §3.4） |
| T13 | oob 上调用 printk | `note_trap`（trap 入口）、看门狗、HM | Z 调 z | printk 是 Linux 代码，Dovetail 保证它能在 oob 上调用 | 全局问题，不只 thread.c；记为后续（§5.4） |
| T14 | Z 对象里嵌着 Linux 对象：`exited`（completion）、`evl_kthread.done`、`altsched`、element 的 `cdev` 等 | `struct evl_thread`、`struct evl_kthread` | — | 同 doc 24 的 Y2' | doc 24 N2 |
| T15 | attach 路径混合了 z 的操作（提升 capability、`set_cpus_allowed_ptr`、`set_task_comm`）和 Z 的操作（分配 sstate、迁移、入队） | `map_uthread_self`、`thread_factory_build` | — | 都在 inband、由线程自己发起，z 的部分只改它自己的 Linux 状态 | 结构问题，和 doc 20 的 factory 拆分一起做 |

---

## 4. 改了什么

### 4.1 sysfs：门 + z 侧格式化（T1、T2）

**原来的样子**（`orig: thread.c:2694-2730`，`sched_show`）：

```c
thread = evl_get_element_by_dev(dev, struct evl_thread);
raw_spin_lock_irqsave(&thread->lock, flags);          /* z 拿 Z 的硬锁 */
...
ret = snprintf(buf, PAGE_SIZE, "%d %d %d %s ", ...);   /* 持锁执行格式化 */
if (sched_class->sched_show)
	_ret = sched_class->sched_show(thread, buf + ret,  /* Z 的回调写 z 的缓冲区 */
				PAGE_SIZE - ret);
...
raw_spin_unlock_irqrestore(&thread->lock, flags);
```

谁都可以读 `/sys/devices/virtual/thread/<名字>/sched`（测试里实际的路径）。每读一次，就会：

- 在 z 的执行流里拿 Z 的 `thread->lock`，并关硬中断；oob 上的调度和唤醒也要拿这把锁；
- 持锁期间执行 `snprintf`，以及 Z 调度类的回调；回调往 `buf` 里写，而 `buf` 是 z 提供的页。

`stats_show` 还会在锁里改 Z 的字段 `thread->stat.lastperiod`，`wchan_show` 在锁里顺着 `wchan->name` 指针格式化。

**现在**（`now: thread.c:2686-2987`）拆成两半：

```c
/* Z 门：拿锁，把值收集到 Z 自己的局部变量，放锁，然后才写出去 */
static void evl_thread_gate_sched(struct evl_thread *thread,
				struct evl_thread_sched_snap *out)
{
	struct evl_thread_sched_snap s;
	...
	raw_spin_lock_irqsave(&thread->lock, flags);
	s.bprio = thread->bprio;  s.cprio = thread->cprio;
	s.cpu = evl_rq_cpu(thread->rq);
	lkl_evl_strncpy(s.class_name, sched_class->sched_name(thread), ...);
	if (sched_class->sched_show)                         /* 写进 Z 的栈，不写 z 的缓冲区 */
		n = sched_class->sched_show(thread, s.class_info, sizeof(s.class_info));
	raw_spin_unlock_irqrestore(&thread->lock, flags);
	*out = s;                                            /* 放锁之后才写调用者的内存 */
}

/* z 侧：只调门、格式化 */
static ssize_t sched_show(...)
{
	thread = evl_get_element_by_dev(dev, struct evl_thread);
	evl_thread_gate_sched(thread, &s);
	evl_put_element(&thread->element);
	ret = snprintf(buf, PAGE_SIZE, "%d %d %d %s ", s.cpu, s.bprio, s.cprio, s.class_name);
	...
}
```

| 属性 | 门 | 门返回什么 |
|---|---|---|
| `sched` | `evl_thread_gate_sched` | CPU、基础优先级、当前优先级、调度类名、调度类附加信息（最多 63 字节） |
| `stats` | `evl_thread_gate_stats` | 4 个计数器、总执行时间、本次统计区间（执行时间、区间长度）；CPU 占用率由 z 计算 |
| `wchan` | `evl_thread_gate_wchan` | 等待通道名字的副本（最多 255 字节） |
| `timeout` | 原有的 `evl_get_thread_timeout` | 本来就只返回一个值 |
| `state`、`pid`、`observable` | 不需要门 | 读的是 Z 发布的值（`READ_ONCE(thread->state)`），或者本来就只返回一个值的 Z 函数 |

门的三条规矩（在源码注释里也写了）：

1. 只传值进出，z 拿不到任何 Z 的指针；
2. Z 持锁期间不碰 z 的内存，也不执行 z 的代码：先收集到 Z 自己的局部变量，放锁之后才写调用者的结构；
3. 格式化、除法这类工作都在 z 侧做。

**`stat.lastperiod` 为什么还留在 Z 里。** 它记录"上一次读 stats 时停在哪里"，下一次读时就能算出这段时间的 CPU 占用率。它只为这个门服务，Z 的任何决策都不读它（`now: grep lastperiod` 只有 `sched/core.c:334` 的初始化和这个门）。如果按"归属单侧"把它搬到 z，就得为每个线程单独分配一个 z 结构，还要处理它和线程的生命周期。现在的做法是：字段留在 Z，**只由门里的 Z 代码写**。z 每读一次就会让它前进一次，影响的只是 z 自己看到的数字，和原来的语义一样。

**它解决了什么、没解决什么。** z 仍然可以频繁读，每次门都会短暂拿一下 `thread->lock`，这和所有的门一样（doc 22 里 xbuf 的门也拿 Z 锁）。区别在于：持锁时间现在是固定的几条赋值，不再由 z 的 `snprintf` 决定，Z 代码也不再往 z 给的地址里写。物理隔离时，这 7 个 `*_show` 可以原样留在 Linux，门变成一次跨域调用。

### 4.2 `pin_to_initial_cpu`：约束 z 给的 CPU 号（T3）

```c
/* orig: thread.c:155-157 */
cpu = task_cpu(p);
if (!cpumask_test_cpu(cpu, &thread->affinity))
	cpu = cpumask_first(&thread->affinity);
...
rq = evl_cpu_rq(cpu);                      /* 拿 cpu 当下标取 per-CPU 运行队列 */
evl_migrate_thread(thread, rq);

/* now: thread.c:153-163 */
cpu = task_cpu(p);                         /* z 的值：输入 */
if (cpu >= nr_cpu_ids || !cpumask_test_cpu(cpu, &thread->affinity))
	cpu = cpumask_first(&thread->affinity);
```

`task_cpu(p)` 是从 Linux 的任务结构里读出来的。原来它越界时，`cpumask_test_cpu` 会在 Z 的位图外面读，读到 1 的话，`evl_cpu_rq(cpu)` 就会把一块任意内存当成运行队列，再把线程迁移过去。现在先检查范围，越界就退回到亲和性掩码里的第一个 CPU。这个值本来就只是"挑个起始 CPU"的提示，用默认值替代不影响正确性。这是 doc 26 的"输入化"。

### 4.3 `evl_killall`：printk 移出硬锁（T4）

原来在遍历线程链表、持有 `thread_list_lock` 的时候，如果开了 EVL 的 debug 选项，会对每个线程 `printk`。这把锁在 `map_kthread_self` 切到 oob 之后的 `enqueue_new_thread` 里也会拿，所以 oob 路径可能要等 inband 的 printk 跑完。现在把打印挪到放锁之后的第二个循环里。那个循环本来就对每个线程持有引用，所以访问 `t->name` 是安全的。

---

## 5. 不改的，以及为什么

### 5.1 Dovetail 事件：EVL 线程就是 Linux 任务（T5、T6）

几个事件的内容：

| 事件 | Linux 什么时候发 | EVL 做什么 |
|---|---|---|
| `INBAND_TASK_SIGNAL` | 给这个任务发信号时（`kernel/signal.c:766`、`993`，持有 `siglock`） | 解除 FREEZE，kick 出 oob，让它回 inband 处理信号 |
| `INBAND_TASK_MIGRATION` | Linux 要把它迁移到别的 CPU 时 | 如果它正睡在 oob 上，就 kick，并要求它返回用户态前回调 |
| `INBAND_TASK_PTSTOP` / `PTCONT` | ptrace 停止、继续时 | 进入 ptrace 状态，请求返回用户态前回调 |
| `INBAND_TASK_RETUSER` | 线程自己返回用户态时 | 切回 oob，处理 ptrace 的同步 |
| `INBAND_TASK_EXIT`、`INBAND_PROCESS_CLEANUP` | 任务退出、exec、释放 mm 时 | 拆除 EVL 线程 |

这些都是 **z 让 Z 做事**，而且 z 想什么时候发就什么时候发。比如反复给一个实时线程发信号，就能让它反复离开 oob。

不截断的理由是：**用户态的 EVL 线程本身就是一个 Linux 进程里的任务**。它的地址空间、页表、文件、信号、生死都归 Linux 管，Linux 不配合，它连代码都跑不了。在这个前提下，Z 能保护的是：

- Z 的内存不被这些事件破坏：回调都是"传一个任务指针，Z 自己拿锁、自己改"的门的形状；
- 这些事件**只针对被点名的那个线程**：信号发给谁，就 kick 谁；迁移谁，就处理谁。

Z 保护不了的是：Linux 不让某个用户态实时线程正常运行。这一点和 doc 26 §1 "放弃 z 的活性"属于同一类。要说明的一点是，效果并不总是局限在被点名的线程上：如果被 kick 的线程正持有一个 EVL 互斥锁，其他等这把锁的实时线程也会被拖慢。EVL 原本就把这种情况当作错误来检测，通过健康监视报告。

真正属于 Z、不受这种影响的，是 **EVL 内核线程**（latmus、hectic、网络栈的收发线程）。它们没有用户态，不会收到信号，也不会被 ptrace。

T6 那个指针（`task_struct` 里 Dovetail 状态中的 `thread`）由 Dovetail 维护，和 `evl_current()` 用的是同一个字段，doc 24 已经把它归入信任基（Y8）。物理隔离时，要像 fd 表那样换成 Z 自己维护的"任务 → 线程"映射，归入句柄表的工作（N1）。

### 5.2 z 发起的控制请求（T7）：需要你决定

> **已决定（2026-11-13）：** 采用方案 ②，只有 EVL 线程能控制 EVL 线程，已实现，见 [30-control-policy.md](30-control-policy.md)。

inband ioctl 能对实时线程做这些事：

| 命令 | 效果 |
|---|---|
| `EVL_THRIOC_SET_SCHEDPARAM` | 改调度类和优先级 |
| `EVL_THRIOC_SET_MODE` / `CLEAR_MODE` | 改模式位（健康监视选项） |
| `EVL_THRIOC_DEMOTE` | 降级为 weak 类并踢出 oob |
| `EVL_THRIOC_SUSPEND` / `RESUME` | 挂起、恢复 |
| `EVL_THRIOC_UNBLOCK` | 打断它当前的等待 |

control 设备上的 `evl_killall` 能取消所有线程。

形式上这些都是门：参数先拷进来，`evl_find_sched_class` 会检查调度参数是否合法，然后在 Z 的锁下修改。问题在于效果：**一个普通 Linux 进程（z）可以合法地降级或挂起一个实时线程。** 前提是它拿得到这个线程的 fd，而公开线程的 fd 谁都可以打开。

有三种做法：

| 方案 | 做法 | 代价 |
|---|---|---|
| ① 维持现状 | 拿得到 fd 就能控制，这是 EVL 原本的语义 | z 能影响实时调度 |
| ② 只允许 oob 调用者改实时属性 | 改调度、demote、suspend 只接受 oob ioctl，也就是调用者必须是 EVL 线程；inband 只能读 | 不是 EVL 线程的管理程序就不能再调整实时线程；libevl 的 `evl_set_schedattr` 在非 EVL 线程里会失败 |
| ③ 只允许线程自己和同进程的 EVL 线程 | 在 ② 的基础上，再用 Z 记录的线程归属（`oob_mm`）做检查 | 更严格，管理工具也会受影响 |

这取决于你的威胁模型里，"控制实时线程"这件事算不算要保护的东西，我没有替你决定。如果选 ② 或 ③，改动集中在 `__thread_ioctl` 和 control.c，不大。

### 5.3 Linux 对象嵌在 Z 对象里（T14）

`exited` 是 Linux 的 completion，`evl_join_thread` 和 `evl_killall` 在 inband 上等它；`evl_kthread.done` 是 P2 用的 completion；`altsched` 是 Dovetail 的结构。它们的处理方法和 monitor 的等待队列一样（doc 25 §5.1），是"归属单侧"，但 `exited` 涉及线程对象的释放时机（`thread_factory_dispose` 里先 `complete_all`，再 `kfree_rcu`），要和 element 的拆分（doc 20 步骤 6、doc 24 N2）一起设计，这次没动。

### 5.4 oob 上的 printk（T13）

trap 入口的 `note_trap`、看门狗、HM 的 `printk_ratelimited` 都可能在 oob 上调用 printk。Dovetail 保证 printk 在 oob 上可以调用（消息写进无锁的环形缓冲区，控制台输出推迟），但它仍然是 Z 调用 Linux 代码，属于出向交集。

做法可以照搬通知中继：Z 把消息写进自己的环形缓冲区，再用中继通知 inband 去打印。但 printk 遍布整个 EVL（还包括所有 `EVL_WARN_ON`），不是 thread.c 一个文件的事，记为后续。

### 5.5 attach 路径的混合（T15）

`map_uthread_self` 在同一个函数里既改 Linux 的东西（提升 `CAP_SYS_NICE` 等 capability、`set_cpus_allowed_ptr`、`set_task_comm`），又改 EVL 的东西（分配 sstate、迁移、入队、release DORMANT）。它整个在 inband、由线程自己发起，z 的部分只改它自己的 Linux 状态，所以没有新的依赖。拆成"z 侧准备 + Z 门"只是结构上的整理，和 doc 20 的 factory 拆分一起做更合适。

---

## 6. 顺带发现的原版问题（没有改）

| # | 问题 | 位置 | 说明 |
|---|---|---|---|
| B4 | `evl_get_thread_state` 把 CPU 号填进了 `state` 字段 | `orig: thread.c:2142`：`statebuf->state = evl_rq_cpu(thread->rq);` | 应该是 `thread->state`。`EVL_THRIOC_GET_STATE`（libevl 的 `evl_get_state()`）拿到的 `state` 其实是 CPU 号，`cpu` 字段又填了一遍 CPU 号。修起来只要一行，但会改变用户看到的值，留给你决定 |
| B5 | attach 失败时线程名泄漏 | `orig: thread.c:2512-2522`、`2624-2633` | `evl_init_thread` 用 `kvasprintf` 分配了 `thread->name`；`map_uthread_self` 失败（比如没有先 `mlockall`）时走 `discard_unmapped_uthread`，它不释放名字，lockdep key 也没注销。正常 libevl 程序会先调用 `mlockall`，所以很少触发 |

编号接着 doc 20 的 B1–B3。

---

## 7. 验证

| 项 | 结果 |
|---|---|
| 构建 | `make O=../build/linux-evl-qemu bzImage`，EVL 代码没有新的警告；objtool 的 `notrack prefix` 警告数量和改动前相同（91 条，与本次改动无关） |
| 回归测试 | 78 个测试通过 74 个，与改动前的基线相同；失败的 4 个也相同（`ptrace-sync` 缺 gdb，`sched-quota-accuracy`、`sched-tp-accuracy`、`sched-tp-overrun` 是配置里没开对应的调度类）。dmesg 没有 WARNING、BUG、Oops |
| sysfs | 在原有测试之后加一个阶段：后台循环读取所有 EVL 线程的 7 个属性，持续 40 秒；同时前台依次跑 `monitor-pi-stress`、`hectic`、`heap-torture`、`clone-fork-exec`、`detach-self`、`thread-suspend`、`thread-demote`、`monitor-event`、`observable-thread`，线程在读的同时不断被创建和退出。结果：40 秒内成功读取 42581 次，全部测试通过，没有内核告警。另有 1309 次读取失败，原因是线程在 `find` 列出目录之后、`cat` 读取之前退出了：线程退出时会先 `evl_hide_element`，此后的读取返回 `-EIO` 或者目录已经不存在，这是预期行为。读出的内容格式与原来一致，例如 `monitor-pi-stress` 的线程：`state=0x4006`，`sched=1 2 2 fifo`，`stats=0 16 46 0 225647 0`，`wchan=&mon->wait_queue`。注意：读取循环固定跑 40 秒，实际只覆盖了 `monitor-pi-stress` 全程和 `hectic` 的前半段，后面几个测试没有和读取并发 |

---

## 8. 代码索引

| 内容 | 原版 | 现在 |
|---|---|---|
| sysfs 门和 z 侧 | `orig: thread.c:2672-2900` | `now: thread.c:2686-2987`（`evl_thread_gate_sched`、`_wchan`、`_stats`，以及各 `*_show`） |
| `pin_to_initial_cpu` | `orig: thread.c:142-175` | `now: thread.c:142-179` |
| `evl_killall` | `orig: thread.c:1474-1530` | `now: thread.c:1481-1539` |
| Dovetail 事件 | `orig: thread.c:1643-2010` | `now: thread.c:1653-2019`（未改） |
| ioctl | `orig: thread.c:2190-2340` | `now: thread.c:2200-2350`（未改） |
| Dovetail 发事件的地方 | `include/linux/dovetail.h:87-129`、`kernel/signal.c:766,993`、`kernel/sched/core.c:3093` | 同 |
