# EVL × LKL 设计与实现记录

目标：把 Xenomai 4 EVL 核心对 Linux 函数的依赖迁移到 LKL 上，由 EVL 为 LKL 提供 host_ops。

## 目录

| 文件 | 内容 |
|---|---|
| [01-goals-and-principles.md](01-goals-and-principles.md) | 目标、三条原则、整体架构设想 |
| [02-object-classification.md](02-object-classification.md) | 对象三分类（可复制 / 交给 EVL / 第三类）及典型例子 |
| [03-function-levels.md](03-function-levels.md) | 函数分级 L0/L1/L2，判定标准与判定方法 |
| [04-calling-lkl.md](04-calling-lkl.md) | EVL 如何调用 LKL：直接调用与进入 LKL 上下文、开销、实时性规则 |
| [05-dependency-inventory.md](05-dependency-inventory.md) | EVL 对 Linux 的依赖盘点，helper 函数 A/B/C 分类 |
| [06-build-pipeline.md](06-build-pipeline.md) | 构建流程：LKL 内核 ABI 编译、段重命名、链接进 EVL |
| [07-testing.md](07-testing.md) | QEMU + initramfs 测试方法，各函数的触发方式 |
| [08-open-questions.md](08-open-questions.md) | 待讨论问题（组会材料） |
| [09-case-oob-irq-work.md](09-case-oob-irq-work.md) | 案例：oob 有状态依赖 `irq_work_queue`（proxy 写路径，三个信箱）|
| [10-irq-work-glossary.md](10-irq-work-glossary.md) | irq_work 路径上的名词、数据结构、函数、标志位状态机 |
| [11-proxy-linux-deps.md](11-proxy-linux-deps.md) | proxy.c 调用的 Linux 函数、读写的 Linux 对象、被 Linux 读写的 EVL 对象；EVL/Linux 域划分的定义讨论 |
| [12-domain-partitioning.md](12-domain-partitioning.md) | 域划分方法：结合 SoK，以代码为中心 + stage 约束的混合模型；数据与函数的判据、门、强制执行，以 proxy.c / work.c 为例 |
| [13-oob-shared-state.md](13-oob-shared-state.md) | 思路梳理：从 LKL 迁移的动机到域划分；oob 任意打断 inband 的根本原则；对 oob 安全的函数和字段协议；无状态/有状态函数；共享状态三类处理（快照、镜像、另建/共享区）；f_op 与 O_NONBLOCK 试验方案 |
| [14-trust-domain-vs-stage.md](14-trust-domain-vs-stage.md) | 可信域与 stage 的关系：可信域 = oob 的 TCB，oob ⇒ 可信、反之不成立；对象按"oob 是否依赖其值"归属；读写、调用、等待规则；函数归属与对象判定算法；对 doc 12、13 的修正清单 |
| [15-trust-closure-pilot.md](15-trust-closure-pilot.md) | 以 oob 依赖为中心的数据驱动划分的第一次试做：clang 导出字段读写和数据流，自动求可信字段闭包和截断点（proxy、work、file、factory、syscall）；复现了 01/02/05；新发现 f_op 函数指针、i_cdev 绑定、fd→element 链等依赖。工具在 tools/trust-closure/ |
| [16-subsystems/](16-subsystems/README.md) | 四个子系统（sem、xbuf、monitor、observable）的功能、原理、逐字段的数据结构、逐函数语义；第二版按**划分规则 1–10**（调用者可信、传值由调用者负责、指针要找全写者、以分配单元的类型为单位、嵌入跟随容器、Linux 成员移出、Linux 对象截断、用户态可写内存不可信）重做划分。主要结论：`evl_element` 要拆分；Linux 等待队列和 irq_work 要移出可信对象；Linux 入口要有门；`evl_ksem` 总是被嵌入（spidev） |
| [17-whole-evl-annotation.md](17-whole-evl-annotation.md) | 按规则 1–10 对当前配置编译的全部 EVL 代码（32 个编译单元、86 个文件、855 个函数、151 个结构体）做自动划分，结论以 `// @TD` 注释追加在源码行尾（行号不变）。kernel/evl 几乎整体可信，工作量在接触面：37 个规则 8 成员、67 个 Linux 入口、约 50 个 Linux 字段；新发现：oob 系统调用经 Linux fd 表找文件、oob 上调用时钟设备驱动的 `set_next_event`、`monitor_oob_write` 在 oob 上用非 raw 的 `copy_from_user` |
| [18-xbuf-split.md](18-xbuf-split.md) | xbuf 拆分方案（第二版）：每个环按端拆开，各域保存自己那一端的下标、预留量和锁，跨域只共享缓冲区和原子 `fillsz`；可信端约束后才使用 `fillsz`，地址只用自己的下标算；不可信 → 可信经门铃，可信 → 不可信经通知中继；门从第一版的 10 个减到 3 个 |
| [19-untrusted-peer-channels.md](19-untrusted-peer-channels.md) | 对端不可信的通道：不保护通道的正确性，只保护可信内存、实时性和控制流；规则 C1–C7；用 proxy 对照，规则成立。proxy 多出三种情形：同一端有两个域的调用者（经门完成整次操作）、`on_error` 成为返回值（要约束）、`oob_poll` 转发（删除）；公共设施（共享区分配器、通知中继、门铃）|
| [20-factory-and-handles.md](20-factory-and-handles.md) | factory.c 分析：element 的生命周期、可见性、绑定；oob 系统调用经 Linux fd 表、`f_op`、`private_data` 找对象（L1）；释放由 Linux 的 irq_work、kthread、RCU 驱动。决定：在可信域维护 per-process 句柄表，句柄只由可信代码创建，libevl 改 ABI；7 步实施顺序；顺带发现 B1、B2 两个 bug |
| [21-roadmap.md](21-roadmap.md) | 路线图：先做 oob 运行底座，P0 基础设施 → P1 信任基 → P2 内存 → P3 时间 → P4a 对象模型基础 → P4b 调度与线程 → P5 系统调用 → P6 同步 → P7 外围（xbuf、proxy 等）→ P8 隔离；"雏形"的定义 T1–T4；每个文件的工作流程 |
| [22-pipe-final-design.md](22-pipe-final-design.md) | xbuf、proxy 的最终设计与实现（可单独阅读）：按功能流程列标记、代入 Z/z 找交集、五种截断方法；每个环拆成可信端 / 不可信端 / 共享部分，可信端自持 `fill′`，z 只经门报告数量并被截断，Z → z 只记录（通知中继）；proxy 去掉 inband 读写；不变式 I1–I6、行为变化、后续任务 N1–N8 |
| [24-core-cut.md](24-core-cut.md) | EVL 核心的截断（已实现）：盘点 9 类 Z → z 通知（线程切 inband、kthread、信号、monitor、observable、stax、evl_work、crossing、element 销毁）全部改为只记录，核心里不再有 `irq_work`；通知中继第二版（合并位、一次性记录、inband 同步投递、drain 代替引用）；Z 读 Linux 状态的 4 处截断（oob 权限不读凭据、`clock_sleep` 重启日期私有副本、信号不再按次分配可信堆、monitor oob 写去掉 `might_fault`）；monitor、observable 的等待队列移出可信对象；修 factory B1–B3；新基线 74/78 与 HEAD 相同；剩余交集 N1–N8（句柄表等） |
| [25-monitor-observable.md](25-monitor-observable.md) | monitor 和 observable 的完整说明（可单独阅读）：功能（同步原语后端、发布/订阅，两者都是 EVL 有意提供的跨域同步点）、流程与标记、原实现的交集 X1–X11（等待队列头嵌入、直接 `wake_up`、irq_work、回调里放引用、flush 入口、`might_fault`、偷看、inband 等待的混合代码等）、修改（归属单侧、只记录、CALL 记录、drain、`access_ok`）、不变式 M1–M6；下一步建议拆开 inband 等待路径（门 + 标准等待写法） |
| [26-zz-method.md](26-zz-method.md) | **方法总结（可单独阅读，建议首先读）**：安全目的与放弃的东西；为什么不能把 Linux 调用换成 LKL 调用（`wake_up`、`fdget` 两个例子）和"先定归属、再定实现"；可信/EVL/实时三个独立维度；Z/z 闭包与唯一规则"Z 不依赖 z"（值、锁、代码、内存、时序、资源），入向/出向，"只写也不行"，两类例外；分析步骤；五种截断方法逐个配例子；常见误区；monitor 完整走一遍；现状与剩余 |
| [27-inband-notify.md](27-inband-notify.md) | Z → z 通知机制（可单独阅读）：为什么 oob 上本来就不能调 Linux、原版用 irq_work "先登记后执行"；原版全部通知点逐个说明（P1–P9、xbuf、proxy，inband 直接调用，mayday/ucall 等其他机制）；现在的中继（记录、闸门、处理函数、drain、ONESHOT）；合并与 irq_work 语义相同、不会丢唤醒的时序论证，真正变化的三处（信号累积、drain 代替引用、inband stall 时推迟）；inband 等待路径的锁嵌套（X8）是什么、为什么、怎么拆 |
| [28-thread.md](28-thread.md) | thread.c 的 Z/z 改写（可单独阅读）：按发起者列出 z → Z 的 5 类入口和 15 处交集 T1–T15；改了 sysfs（门 + z 侧格式化，z 不再持 Z 锁、Z 回调不再写 z 缓冲区）、`pin_to_initial_cpu` 约束 z 给的 CPU 号、`evl_killall` 的 printk 移出硬锁；不改的理由：EVL 线程就是 Linux 任务（Dovetail 事件）、z 发起的控制请求是策略问题（三个方案待定）、嵌入的 Linux 对象归 N2、oob printk 是全局问题；原版 bug B4（GET_STATE 的 state 填成 CPU 号）、B5（attach 失败泄漏线程名） |
| [29-notify-relay.md](29-notify-relay.md) | **通知中继详解（当前实现，可单独阅读）**：oob 为什么不能调 Linux，irq_work 是"跳板"、workqueue 是第二块跳板；Z 记录 + per-CPU 链表 + `irq_post_inband` + z 侧处理函数经两个闸门取副本的整体结构，与 Linux `raised_list` 的对照；monitor 一次完整通知；去掉 `waitqueue_active()` 改为无条件 post 的理由与代价；闸门只交值不交地址（`fn` 在 take 里复制，done 不带参数、Z 用 per-CPU 变量记录）；五种动作、`evl_call_inband` 两跳；drain 与 ONESHOT；Z/z 边界检查表 |
| [30-control-policy.md](30-control-policy.md) | **谁能控制 EVL 线程（可单独阅读）**：安全假设"EVL 线程都可信、其他任务不可信"；EVL 原来"拿到 fd 就有权限"与 Linux 凭证检查的对比；规则 `evl_may_control()` = 调用者是 EVL 线程；盘点 z 的全部控制入口并改 8 处（线程 7 个控制 ioctl、TP/quota 控制、quota 周期、gravity 的 sysfs 和 latmus、时钟设时、有用户线程时拒绝停核心）；对用户态和工具的影响；不改的：Linux 自己的信号/ptrace/亲和性、数据通道、查询；剩下的问题：谁能 attach |
| [commits/](commits/README.md) | 内核四个提交与 libevl 两个提交的逐提交说明、配套关系和验证范围 |
| [31-project-handover-2026-11-16.md](31-project-handover-2026-11-16.md) | 接手时的项目理解与状态快照；后续收尾见 commits 与进度日志 |
| [impl-shared-state/](impl-shared-state/) | 按 13 §11 改造 proxy.c / work.c 的设计与修改记录：删除 `proxy_oob_poll` 对目标文件的转发、`O_NONBLOCK` 镜像、oob → inband 中继另建一份（含补丁） |
| [log.md](log.md) | 进度日志 |
| [figs/](figs/) | 图（`figs/ref/` 是风格参考图）：`build-and-helper.svg` 编译流程与 helper 迁移；`oob-irq-work.svg` oob → inband 交接；`irq-work-fields.svg` irq_work 路径的函数归属与字段读写（`src/gen_fields.py` 生成）|

## 环境

| 项目 | 位置 |
|---|---|
| LKL 源码 | `~/xenoami/lkl`，分支 `evl-glue`，镜像 [KuanKuanQAQ/lkl](https://github.com/KuanKuanQAQ/lkl) |
| linux-evl 源码 | `~/xenoami/linux-evl`，分支 `evl-lkl`（基于 `v6.12.y-cip-evl-rebase`），镜像 [KuanKuanQAQ/linux-evl](https://github.com/KuanKuanQAQ/linux-evl) |
| libevl 源码 | `~/xenoami/libevl`，镜像 [KuanKuanQAQ/libevl](https://github.com/KuanKuanQAQ/libevl) |
| 上游 | lkl: github.com/lkl/linux；Xenomai 4: gitlab.com/Xenomai/xenomai4（官方 source.denx.de） |
| 宿主机 | x86_64，gcc 11.4，128 核，可用 KVM |

构建目录都在 `~/xenoami/build/`：

| 目录 | 用途 |
|---|---|
| `build/lkl` | LKL 默认配置（用户态 ABI），只用于 clangd 跳转 |
| `build/lkl-kabi` | LKL 裁剪配置 + 内核 ABI，产出放进 EVL 的 `lkl.o` |
| `build/linux-evl` | `x86_64_defconfig` + EVL，只用于 clangd 跳转 |
| `build/linux-evl-full` | 打开全部 EVL 选项，只编译 `kernel/evl/` 和 `drivers/evl/`，用于依赖盘点 |
| `build/linux-evl-qemu` | 放进 QEMU 运行的宿主内核 |
| `build/libevl`、`build/libbpf` | libevl（meson）及其依赖的 libbpf（从 linux-evl `tools/lib/bpf` 编译）|
