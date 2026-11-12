# 按 doc 13 §11 改造 proxy.c / work.c：设计与修改记录

> **2026-11-09：本目录记录的修改已全部撤回。** 代码存为补丁，放在 [patches/2026-11-09-shared-state-wip/](../../../patches/2026-11-09-shared-state-wip/README.md)，工作区已回退到修改之前。撤回的原因见那里的 README：方法从"按 stage 划分"改成了"按 oob 依赖划分"（doc 14、15），决定从原始代码重新分析。下面的内容保留作为探索记录。


本目录记录按 [13-oob-shared-state.md](../13-oob-shared-state.md) §11"共享状态的三类处理"对 `kernel/evl/proxy.c` 和 `kernel/evl/work.c` 所做的修改。每篇先介绍**原本的机制和调用的函数**，再介绍**修改思路**和**具体修改**。

- 修改时间：2026-11-03 首版，2026-11-04 修订 01，2026-11-05 实现 05。
- 代码位置：`~/xenoami/linux-evl`，分支 `evl-lkl`，**未提交**。

## 三项修改

| 文档 | 处理的共享状态 | 处理方式 | 是否改变行为 | 是否改 Linux |
|---|---|---|---|---|
| [01-drop-oob-poll-forward.md](01-drop-oob-poll-forward.md) | 目标文件的 `f_op->oob_poll`（`proxy_oob_poll` 每次都读，并间接调用） | **不访问**：删除 oob poll 对目标文件的转发 | **是**：poll proxy 不再合并目标文件的 oob 事件 | 否 |
| [02-nonblock-mirror.md](02-nonblock-mirror.md) | proxy 文件自己的 `f_flags & O_NONBLOCK`（oob 读写路径每次都读） | §11 第二类：Linux 低频写 → EVL 存镜像，Linux 推送 | 否 | 是，2 处推送点 + 1 个 Dovetail 弱钩子 |
| [03-relay-split.md](03-relay-split.md) | Linux 的 per-CPU irq_work 队列 `raised_list`（`evl_call_inband_from` 每次 oob → inband 交接都要用） | §11 第三类：EVL 只是借用的服务 → 另建一份 | 否 | 否（只用 Dovetail 已有接口） |

阅读顺序建议：01 → 02 → 03 → 05。

## 第四项修改：proxy 拆成 EVL 侧和 Linux 侧

- 设计稿：[04-proxy-bridge-design.md](04-proxy-bridge-design.md)。
- **实现和逐处说明：[05-proxy-bridge-impl.md](05-proxy-bridge-impl.md)。建议直接读 05**：先讲结构体怎么拆、为什么这样拆，然后每个函数单独一节。
- 内容：`relay_output` / `relay_input` 和 proxy 的 inband 文件操作挪到新文件 `kernel/evl/proxy_bridge.c`；它们只能通过 `proxy.c` 里的 `proxy_gate_*` 门访问 EVL 状态，由编译器保证。
- 行为不变。读代码时发现的疑点 P1–P6 原样保留，见 04 §1.6。

### 01 的修订经过

01 最初的做法是"把目标文件的 `f_op->oob_poll` 存成快照"（§11 第一类，不改变行为）。讨论中发现：

1. proxy 的 `oob_read` / `oob_write` 只看 ring，数据总是由 inband 的 `kernel_read` / `kernel_write` 转发，从来不走目标文件的 oob 路径，所以目标文件的 oob 就绪状态和 proxy 能否读写无关；
2. `kernel_read` / `kernel_write` 要求目标实现 `read_iter` / `write_iter`，EVL 设备用的是 `read` / `write`，所以 proxy 本来就包不了 EVL 设备；
3. 真正会走到这一步的是 socket（打开 `CONFIG_EVL_NET` 后，所有 socket 都带 `sock_oob_poll`），而原代码把 proxy 自己的 `filp` 传了过去，会让 `sock_oob_poll` 把 proxy 的内存当成 `evl_socket`，调用一个乱指的函数指针。

所以改为直接删除这一步转发。推理过程和被替代的快照方案都记在 01 里。第 3 点是读代码得出的，尚未运行复现。

## 改动的文件

| 文件 | 属于 | 改动 |
|---|---|---|
| `kernel/evl/proxy.c` | 01、02 | 01：删除 `proxy_oob_poll` 末尾对目标 `oob_poll` 的调用，换成说明注释。02：新增 `proxy_nonblock()`，三处 oob 可达的 `O_NONBLOCK` 判断改读镜像 |
| `include/evl/file.h` | 02 | `evl_file` 加 `nonblock` 镜像字段和 `evl_file_nonblock()` |
| `kernel/evl/file.c` | 02 | `evl_open_file` 初始化镜像；实现 Dovetail 钩子 `handle_inband_fflags_change()` |
| `include/dovetail/fs.h` | 02 | 声明钩子 `handle_inband_fflags_change()`（`!CONFIG_DOVETAIL` 时为空内联） |
| `kernel/dovetail.c` | 02 | 钩子的 `__weak` 默认实现（空） |
| `fs/fcntl.c` | 02 | `setfl()` 在 `f_lock` 内调用钩子 |
| `fs/ioctl.c` | 02 | `ioctl_fionbio()` 在 `f_lock` 内调用钩子 |
| `include/evl/work.h` | 03 | `evl_work` 的 `irq_work` 换成 `relay_node` + `relay_flags`；`evl_flush_work` / `evl_cancel_work` 改用 `evl_sync_relay()` |
| `kernel/evl/work.c` | 03 | EVL 自己的 per-CPU 中继链表 + Dovetail 合成中断，替代 `irq_work_queue` |
| `kernel/evl/init.c` | 03 | `init_core()` 里初始化和清理中继 |
| `kernel/evl/proxy.c` | 05 | 去掉转发者和 inband 文件操作，新增 15 个门 |
| `kernel/evl/proxy_bridge.c`（新） | 05 | Linux 侧：两个转发者、4 个 inband 文件操作、bridge 的创建和销毁 |
| `kernel/evl/proxy_gate.h`（新） | 05 | 两侧之间的函数声明 |
| `kernel/evl/Makefile` | 05 | 加 `proxy_bridge.o` |

01–03 共 10 个文件，+223 / −21 行；加上 05，共 13 个文件（含 2 个新文件）。完整补丁在 [patches/](patches/)：

| 补丁 | 内容 |
|---|---|
| `patches/all.diff` | 全部修改，01–03 和 05（不含 `kernel/evl/syscall.c`，见下） |
| `patches/proxy.c.diff` | proxy.c 里 01、02 的修改（05 之前的版本）：最后一个 hunk 属于 01，其余属于 02 的 EVL 一侧 |
| `patches/proxy-bridge.diff` | 05：相对于 01、02 之后的 proxy.c，加上 Makefile 和两个新文件 |
| `patches/nonblock-mirror-core.diff` | 02 的其余部分（Dovetail 钩子、fs 推送点、evl_file 镜像） |
| `patches/relay-split.diff` | 03（work.h、work.c、init.c） |

> **注意**：工作区里 `kernel/evl/syscall.c` 第 1 行有一处不是本次修改的改动（`/*` 变成了 `++/*`），会导致该文件编译失败。本次没有动它，也没有包含进补丁，请自行确认是否需要恢复（`git checkout kernel/evl/syscall.c`）。

## 验证状态

| 项目 | 结果 |
|---|---|
| 编译：`build/linux-evl-qemu` 中改动涉及的目标文件 | 通过，无警告 |
| 编译：`build/linux-evl-full`（含 `CONFIG_EVL_NET`）中除 `syscall.c` 外的全部 EVL 目标文件，以及 `fs/fcntl.o`、`fs/ioctl.o`、`kernel/dovetail.o` | 通过，无警告。`kernel/evl/net/skb.c` 的 `EVL_DEFINE_WORK` 静态初始化也编译通过 |
| 静态检查：改动前后 `proxy.o` 中 oob 函数对 `struct file` 字段的访问 | 改动前有 4 处（01 一处 `f_op`，02 三处 `f_flags`），改动后为 0 |
| 静态检查：`proxy_oob_poll` 的调用 | 改动前有一次间接调用（`call *%rax`，即目标的 `oob_poll`），改动后没有 |
| 静态检查：`proxy.o` 的外部符号 | 改动前后完全相同 |
| 静态检查：`work.o` 的外部符号 | `irq_work_queue` 消失；新增 `irq_post_stage`、`inband_stage`、`llist_add_batch` 等，逐个分析见 03 §4.2 |
| 链接 bzImage、QEMU 运行测试 | **未做**。bzImage 链接本身还在待办里（见 [log.md](../log.md)），运行测试计划见各篇的"验证"一节；01 的 socket 场景复现需要打开 `CONFIG_EVL_NET` 的内核 |

## 没有做的事

- `kernel/evl` 里还有其他代码直接调用 Linux 的 `irq_work_queue`（`xbuf.c`、`monitor.c`、`observable.c`、`sched/core.c`、`factory.c` 的 `__evl_put_element`），以及 Dovetail 自己的 `schedule_inband_fput`。本次只改了 `evl_work` 这一条路径，其余可以按 03 的方式逐个迁到 EVL 的中继上，见 03 §8。
- proxy 以外还有 9 个 EVL 源文件、共 19 处用到 `O_NONBLOCK`，本次没有改。镜像已经对所有 EVL 文件生效，其中 oob 可达的那些只需把 `filp->f_flags & O_NONBLOCK` 换成读镜像。proxy 自己在 inband 函数 `proxy_write`、`proxy_read` 里的两处也保持原样（只在 inband 上运行的函数不管）。
- inband 版 `proxy_poll` 也会合并目标文件的普通 `poll` 结果，但那是有道理的：inband 的 `proxy_read` / `proxy_write` 会在调用者上下文里同步访问目标文件，而且 inband 版不做预读，要靠目标的等待队列叫醒 poller（见 01 §2.4）。它唯一的小问题是合并时不按方向过滤，本次不动。
