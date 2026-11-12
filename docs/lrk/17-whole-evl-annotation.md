# 17 全量划分：kernel/evl 的全部结构体、字段、函数，注释在源码行尾

> 2026-11-10。按 [16-subsystems/README.md](16-subsystems/README.md) 的规则 1–10，对当前配置（`build/linux-evl-qemu`）实际编译的全部 EVL 代码做自动分析，结论以注释的形式追加在源码行尾。
> 工具：`~/xenoami/tools/trust-closure/`（`run_all.sh` → `analyze_all.py` → `annotate.py`）；汇总在 `out/all-result.txt`。
> **2026-11-10 更新**：注释已从工作区回退，源码里不再有 `// @TD`；需要时用补丁重新打上。
> 注释的补丁副本：`~/xenoami/patches/2026-11-10-trust-annotations/annotations.patch`。这是自动分析的结果，**没有运行验证**，局限见 §6。

## 1. 范围

| 项目 | 数量 |
|---|---|
| 编译单元 | 32 个：`kernel/evl/*.c`、`kernel/evl/sched/{core,fifo,idle,weak}.c`、`kernel/evl/lib/*.c` |
| 注释的文件 | 86 个（上面的 `.c` 加上它们 include 的 EVL 头文件：`include/evl`、`include/uapi/evl`、`include/asm-generic/evl`、`arch/x86/include/asm/evl`） |
| 注释的行 | 1580 行，每处都只在原行尾追加，**行号不变** |
| 函数 | 855 个定义（含头文件里的内联函数） |
| 结构体 / 联合体 | 151 个（含匿名成员） |

**没有覆盖**：当前配置不编译的代码，包括 `sched/quota.c`、`sched/tp.c`、`kernel/evl/net/`，以及各文件里未启用的 `#ifdef` 分支（约 63 个函数、20 个结构体，比如 `CONFIG_EVL_WATCHDOG` 关闭时的 `watchdog_handler`）。`drivers/` 里使用 EVL 的驱动（spidev、latmus 等）也没有分析。

## 2. 注释格式

所有注释都以 `// @TD` 开头，三种行各有格式。

**结构体定义行**（`struct x {`）：

```
// @TD [放置] 依据 创建：位置… 栈上：位置… 嵌入于：容器.字段(放置)… 嵌入在 Linux 对象 X 里…
```

**字段行**：

```
// @TD 字段名: [放置] 备注；读：位置…；写：位置…
```

**函数定义行**（函数名所在的行）：

```
// @TD [oob:是/可能/否/否？] 原因 [可信/不可信] 原因 [Linux 入口：…]
```

放置标签：

| 标签 | 含义 |
|---|---|
| `[可信]` | 可信分配单元，或者嵌在可信容器里 |
| `[不可信]` | oob 不依赖它，在数据闭包之外 |
| `[不可信（用户共享内存，规则10）]` | 分配在 `evl_shared_heap`，用户态可以直接写 |
| `[用户输入（规则10/3）]` | uapi 类型，只作为用户输入的栈副本出现 |
| `[可信→应移出（规则8…）]` | 可信对象里嵌入的、会被 Linux 函数写的成员 |
| `[可信→Linux 锁（doc14…）]` | 可信对象里的 Linux 锁（`mutex`、`spinlock_t` 等），由 Linux 决定谁进临界区 |
| `[匿名成员，并入 X]` | 匿名 struct/union，它的字段算作 X 的字段 |

字段备注：

| 备注 | 含义 |
|---|---|
| `闭包第1步` / `闭包第2步` | oob 函数直接读它 / oob 依赖的字段，它的写者为了算出值而读它 |
| `嵌入 T，随容器` | 规则 6 |
| `指向 Linux 对象 T；oob 闭包含其字段 …（规则9：需截断）` | 可信对象经这个指针读了 Linux 对象的字段 |
| `指向用户共享内存 T（规则10…）` | 例如 `evl_monitor.sstate` |
| `目标：f1、f2…` | 函数指针字段，存进去的函数 |
| `Linux 操作：fn@位置…` | 拿到这个成员地址的 Linux 函数（规则 8 的依据） |

读写位置的写法：

- 格式是 `文件:行号`。`kernel/evl/` 前缀省略，`include/` 前缀省略。
- 后缀 `*` 表示这一处在**确定的 oob 函数**里，`?` 表示在**可能的 oob 函数**里（见 §3.1），没有后缀表示 inband 或在 `running_inband()` 分支里。
- 可信字段最多列 5 处，带 `*` 的优先，后面写"等共 N 处"。**不可信的字段列出全部读**，这是你要求的。
- "写"包括赋值、`++`/`--`、加锁，以及把地址传给可能写它的函数。

函数的可信原因可能同时有几条：

- `在 oob 上运行`
- `写可信字段 X.f@行`
- `调用可信函数 g@行（规则1）`：调用者必须可信
- `被可信函数 g 调用（…；可信代码不能调用不可信代码）`：你最早定的规则

`[Linux 入口：…，可信代码需要门（规则1）]` 表示这个可信函数是由 Linux 调用的：fops 的 inband 成员、`irq_work` 回调、`work_struct` 回调、`call_rcu` 回调、initcall 等。

## 3. 方法

### 3.1 oob 函数

**确定的根**（按来源）：

| 来源 | 数量 | 例子 |
|---|---|---|
| 存进 `file_operations.oob_read/write/ioctl/poll` 的函数 | 27 | `xbuf_oob_read`、`monitor_oob_ioctl` |
| Dovetail 钩子 | 6 | `handle_oob_syscall`、`handle_pipelined_syscall`、`handle_oob_trap_entry/exit`、`handle_oob_mayday`、`resume_oob_task` |
| 用 `IRQF_OOB` 注册的中断处理 | 2 | `oob_reschedule_interrupt`、`clock_ipi_handler` |
| tick 代理设备的 `handle_oob_event` | 1 | `evl_core_tick` |
| `run_oob_call` 的回调 | 1 | `__evl_schedule` |
| `evl_kthread.threadfn` | — | 内核线程体（kernel/evl 里没有，在驱动里） |
| `evl_switch_oob()` 之后的调用 | 若干 | `do_inband_syscall` 里切到 oob 后的 `invoke_syscall` |

**可能的根**：导出函数（`EXPORT_SYMBOL`），没有 inband 证据，也没有作为 Linux 的 inband 回调登记。它们在 kernel/evl 外的调用者（驱动）没有分析，所以标"可能"，可信性按 oob 处理。

**传播**：沿调用边传播，但有三种情况要排除或处理：

- `running_inband()` 分支里的调用不传播。
- `dovetail_resume_inband()` 之后、无条件 `evl_switch_inband()` 之后的调用不传播，另有一条人工规则：`handle_oob_syscall` → `do_inband_syscall`。
- 遇到有 **inband 证据**的函数就停下，记为"冲突"。inband 证据指：调用会睡眠的函数（`mutex_lock`、`schedule`、`copy_from_user`、`wait_for_completion`、`kthread_*`、`device_*` 等），`GFP_KERNEL` 分配，或者 `inband_context_only()`。

**调用图**：

- 直接调用按编译单元解析，`static` 函数优先在同一编译单元内查找。
- 经字段的间接调用，目标 = 存进这个字段的函数（来自初始化列表和赋值）。如果这个字段还被赋过非常量的值（例如 `evl_timer.handler` 由参数赋值），就再加上签名相同的、取过地址的函数。
- 经局部变量或参数的间接调用，按签名匹配。

### 3.2 数据闭包、放置、可信代码

- **数据闭包**：
  - 第 1 步：oob 函数（含"可能"）读的字段。
  - 第 2 步：这些字段的写者为了算出值、地址、控制条件而读的字段，经局部变量、被调用函数的返回值、调用者传入的参数追溯。
- **放置**（规则 5、6、7、10）：
  - 一个类型被依赖，指它自己的字段在闭包里，或者嵌在它里面的 EVL 类型被依赖。
  - 有创建点的类型（分配、全局变量）按"是否被依赖"决定放在可信域还是不可信域。
  - 只被嵌入的类型跟随容器。
  - 分配在 `evl_shared_heap` 的类型是用户共享内存。
  - uapi 类型是用户输入。
- **可信代码**，按下面的规则不断传播，直到不再变化：
  - oob 函数，以及"可能"的 oob 函数；
  - 写可信字段的函数（规则 8 要移出的成员不算）；
  - 可信函数的调用者（规则 1）；
  - 被可信函数调用的 EVL 函数（可信代码不能调用不可信代码）。

## 4. 总体结果

| 项目 | 结果 |
|---|---|
| 函数 | 855：确定 oob 445，可能 oob 87，可信 816，不可信 39 |
| 不可信函数 | 只有两类：只由 Linux 调用、只操作 Linux 对象的回调（`resume_inband_reader/writer`、`wakeup_inband_waiters`、`inband_task_wakeup` 等），以及没有调用者的辅助函数和 sysfs 显示函数 |
| 类型（不含匿名成员） | 141：可信 82，用户输入 39，不可信 13，用户共享 7 |
| 闭包字段 | 438 |
| 规则 8 成员（可信对象里 Linux 写的成员） | 37，其中 3 个是 Linux 锁 |
| 由 Linux 调用的可信函数（需要门） | 67 |
| 冲突（oob 可达但有 inband 证据） | 3 |

**结论**：按"可信代码不能调用不可信代码"和"可信函数的调用者必须可信"这两条，kernel/evl 几乎整体都在可信域里。真正可以放进不可信域的代码，只有那些由 Linux 调用、只操作 Linux 对象的回调。所以划分的工作量不在"把 EVL 代码分开"，而在 EVL 和 Linux 的**接触面**：

- 37 个规则 8 成员要移出可信对象；
- 67 个 Linux 入口要加门；
- 约 50 个 Linux 字段进入了闭包，要截断（§5.3）。

## 5. 主要发现

### 5.1 冲突（3 个）

| 函数 | 调用者 | 证据 | 解读 |
|---|---|---|---|
| `monitor_common_write` | `monitor_oob_write`（oob 写） | `copy_from_user()`@monitor.c:1142 | **真问题**：oob 写路径用了非 raw 的 `copy_from_user`，它会做 `might_fault` 检查。别的 oob 路径都用 `raw_copy_from_user` |
| `inbound_wait_input`、`outbound_wait_output` | `do_xbuf_read/write` 经 `ring->wait_input/wait_output` | `prepare_to_wait_event`、`schedule` | 误报：工具按类型区分字段，两个 ring 的函数指针合在一起。实际 oob 读只走 outbound 那组 |

### 5.2 规则 8 成员（37 个）：EVL 核心层面要统一处理

| 类别 | 成员 |
|---|---|
| element 的 Linux 部分 | `evl_element.rcu/cdev/irq_work/hash`、`evl_factory.cdev` |
| Linux 等待队列头 | xbuf 2 个、monitor 2 个、observable 2 个、proxy 2 个、`evl_stax.inband_wait` |
| irq_work | xbuf 2 个、monitor 2 个、observable 3 个、`evl_work.irq_work`、`evl_crossing.irq_work`、`evl_kthread.irq_work`、`evl_stax.irq_work`、`sig_irqwork_data.work` |
| workqueue、RCU | `evl_work.wq_work`、`evl_thread.inband_work`、`evl_cache_entry.rcu`、`evl_hash_table.rcu` |
| completion | `evl_crossing.passed`、`evl_kthread.done`、`evl_thread.exited` |
| Dovetail 上下文切换 | `evl_thread.altsched`：由 Dovetail 在切换时写，属于运行时底座 |
| **Linux 锁** | `evl_factory.hash_lock`、`evl_cache.lock`、`proxy_ring.worker_lock`：Linux 的锁保护可信数据（doc 14 的问题） |

### 5.3 进入闭包的 Linux 字段（规则 9），按来源

| 来源 | 字段 | 说明 |
|---|---|---|
| fd → 文件 → 对象 | `fd.word`、`oob_file_state.data`（`file.c:42`）、`file.f_op/f_mode/f_flags/private_data`、`file_operations.oob_*`、`inode.i_cdev` | **oob 系统调用经 Linux 的 fd 表找文件**：`evl_get_file` 用 `CLASS(fd)`（Dovetail 允许 oob 调用 `fget`），再读 `filp->f_oob_state`。比 doc 15 说的"三个 Linux 指针"更深一层：连 fd 表本身都是 Linux 的 |
| 系统调用权限检查 | `task_struct.cred`、`kernel_cap_t.val`、`task_struct.comm`（`syscall.c:278-280`） | oob 系统调用是否允许，由 Linux 的凭据（`CAP_SYS_NICE`）决定 |
| tick 编程 | `clock_event_device.mult/shift/min_delta_ns/max_delta_ns/features/set_next_event`、`clock_proxy_device.real_device`（`tick.c`） | **oob 上调用 Linux 时钟设备驱动的 `set_next_event`**。定时器硬件的编程依赖 Linux 的设备驱动，是硬件抽象层的问题 |
| 线程和 trap | `task_struct.ptrace`（`thread.c:1702`）、`restart_block.fn/expires`（`clock.c:490`）、`thread_info.status`、`dovetail_altsched_context.task` | per-task 状态，运行时底座 |
| 用户 I/O 向量 | `iovec`、`compat_iovec`（`lib/uio.c`） | 用户输入的内核副本 |
| 元素创建（inband） | `task_struct.mm`、`inode.i_uid`、`cred.euid` | 进入闭包是因为它们决定了可信字段的值（`clone_flags`、`kuid`） |
| 通用容器 | `list_head`、`rb_node`、`rb_root`、`raw_spinlock` | 嵌在可信对象里，由内联辅助函数操作，随容器 |

### 5.4 共享内存（规则 10）

`__evl_monitor_sstate`、`__evl_thread_sstate` 都分配在 `evl_shared_heap` 里。它们的字段注释列出了内核里**全部**的读位置，方便逐处检查是否校验过（monitor.md §5.2 的表）。

## 6. 局限

| 局限 | 影响 |
|---|---|
| 字段按类型区分，不区分实例 | xbuf 两个 ring 的函数指针合在一起，产生 2 个冲突误报 |
| 静态初始化（全局变量的 `= {...}`）不算"写" | `evl_factory.name/fops` 等字段的写显示为"无" |
| 把地址传给函数一律算作可能写 | 例如 `evl_lookup_ns(&evl_core_ns.m_monitor)` 被记为写 `m_monitor`，写者列表偏大 |
| 用字段名反查 `typeof(*p)` 的类型 | `wprio` 在 `evl_thread` 和 mutex 的 boost 结构里都有，无法确定，留在了 Linux 字段表里 |
| Linux 内联辅助按函数名白名单识别 | 链表、锁、原子、cpumask 操作当作"随容器"，其余 Linux 函数一律当作会写（规则 8 的依据），可能多报 |
| inband 证据按函数名列表识别 | 列表外的睡眠函数会漏，导致 oob 集合偏大（这是保守的方向） |
| 函数中途切换 stage | 只处理 `dovetail_resume_inband`、无条件的 `evl_switch_inband`、`evl_switch_oob` 和一条人工规则。有条件的切换仍可能误判 |
| 导出函数的外部调用者 | 没有分析驱动，导出函数标为"可能 oob" |
| 栈对象 | 列出了栈上的创建点，但地址逃逸（如 `evl_mask_wait`）没有自动识别 |

## 7. 重跑和清除

```
cd ~/xenoami/tools/trust-closure
TMPDIR=<临时目录> JOBS=16 ./run_all.sh          # 约 10 分钟
python3 analyze_all.py out/all out/all-result
python3 annotate.py out/all-result.ann.json      # 先清旧注释再写新注释
./check_syntax.sh                                # 用编译参数逐个编译单元做语法检查
python3 annotate.py --strip                      # 清除全部注释
```

源码里的注释是工作区修改，没有提交。`git diff` 里应该只有行尾追加的 `// @TD` 文本。
