# 21 路线图：从 oob 运行底座开始

> 2026-11-11。在 18–20 之后调整方向：先做 EVL 的基础，尽快得到一个能完整运行的雏形；xbuf、proxy 这类边缘组件放到后面。

---

## 0. 对方向的看法

同意"从 oob 运行所依赖的基础开始"。补充四点：

1. **先划定信任基**。中断、调度、上下文切换都要经过 Dovetail 和体系结构代码。它们算不算可信，决定了每一层怎么改。例如 tick 在 oob 上调用 Linux 时钟设备驱动的 `set_next_event`（doc 17 §5.3），这个驱动要么进信任基，要么被替换。这个问题不先回答，后面每一层都要停下来回头问。
2. **自底向上，但对象模型要提前**。thread 本身就是 element，oob 系统调用的入口经过 factory 和 file。所以 factory 和句柄表（doc 20）的基础部分要放在 thread 之前或者同时做，不能拖到最后。
3. **每个阶段都有"能运行"的判据**：当前 HEAD 能构建、能启动；libevl 测试结果不比基线差；分析工具检查"oob 闭包里没有不可信函数"，并且这个检查的范围随阶段扩大。
4. **xbuf 的工作没有白做**。通知中继（`inband_notify.c`）是基础设施：可信对象里一共有 14 个 `irq_work`（doc 17 §5.2），thread、factory、crossing 都要用它。xbuf 和 proxy 剩下的工作放到阶段 7。

### 0.1 "雏形"的定义

**逻辑上的划分**先做，**物理上的隔离**后做。雏形满足：

- **T1**：有一个明确的可信代码集合 T（按函数列出）。T 中的代码在 oob 上只调用 T 和信任基。
- **T2**：T 的状态，不经过门就只能被 T 写。
- **T3**：Linux 进入 T 的入口都是有名字的门。
- **T4**：libevl 全部测试的结果不比基线差。

T1–T3 用 `tools/trust-closure/` 自动检查。现在它输出的是划分建议，以后改成按配置的可信集合做验收。

---

## 1. 阶段

| 阶段 | 内容 | 主要文件 | 产出 | 判据 |
|---|---|---|---|---|
| **P0 基础设施** | 当前 HEAD 能构建、启动；测试脚本；验收检查 | 构建流程、`tools/` | 一条命令完成"构建 → 打包 initramfs → QEMU → 对比基线" | 当前 HEAD：67 通过，10 失败（§3） |
| **P1 信任基与运行底座** | 列出 EVL 在 oob 上依赖的 Dovetail 和体系结构接口，逐项决定：进信任基、搬进可信域，还是替换 | `include/asm-generic/evl/*`、`arch/x86/include/asm/evl/*`、`kernel/dovetail.c`、`kernel/irq/pipeline.c`（接口部分）、`init.c`、`tick.c` 的硬件部分 | 信任基清单（doc 23） | 清单覆盖 doc 17 §5.2、§5.3 里所有"运行时底座"项 |
| **P2 内存** | 可信分配器、共享区分配器；可信对象不再用 `kzalloc` | `memory.c`、`lib/cache.c`，以及各 `build` 里的分配 | 分配器接口；可信对象全部从可信堆分配 | 全部测试通过；工具检查可信对象的分配点 |
| **P3 时间** | 时钟、定时器、tick | `clock.c`、`timer.c`、`tick.c` | 时钟读取和定时器硬件编程不经过不可信代码 | `clock-timer-periodic`、`sched-*`、latmus |
| **P4a 对象模型基础** | 可信句柄表；`evl_element` 的可信部分；put 改用通知中继 | `factory.c`、`file.c`、`syscall.c`、`lib/map.c`、`namespace` | doc 20 的步骤 0–3 | `element-visibility`、`duplicate-element`、`file-dup`、`simple-clone` |
| **P4b 调度与线程** | 调度器；线程生命周期；stage 切换；信号、ptrace；`evl_kthread`；当前线程由 `this_evl_rq()->curr` 得到（doc 20 §6.4） | `sched/*.c`、`thread.c`、`wait.c` | thread.c 拆成可信核心和 Linux 胶水 | `thread-*`、`detach-self`、`clone-fork-exec`、`ptrace-sync` |
| **P5 系统调用和控制** | oob 系统调用入口只用句柄；权限检查不读 Linux 的 `cred`；control 设备 | `syscall.c`、`control.c` | doc 20 的步骤 4–7 | 全部测试 |
| **P6 同步** | 内核 mutex、monitor（和用户态共享的 sstate，规则 10）、stax、sem | `mutex.c`、`monitor.c`、`stax.c`、`sem.c` | 共享状态全部当输入处理 | `monitor-*`、`sem-*`、`rwlock-*`、`stax-*` |
| **P7 外围** | poll、observable、xbuf（完成步骤 4）、proxy、trace、random、net、驱动 | 相应文件 | 按 C1–C7 和句柄表改完 | 全部测试 |
| **P8 隔离** | 可信内存区、地址空间或保护键、真正的门；LKL 的位置 | — | 另行规划 | — |

**顺序的理由**：

- P2 在 P3、P4 之前：所有可信对象都要从可信堆分配，后面每一层都会用到。
- P4a 在 P4b 之前：thread 是 element，线程的 efd、`__evl_current_efd` 都经过 factory。
- P6 在 P4b 之后：monitor 建在 `evl_mutex` 和线程的等待机制上。
- P7 放在最后：xbuf、proxy、poll、observable 都只是用前面各层提供的东西。

---

## 2. 每个文件的工作流程

1. **盘点**：用工具（`tools/trust-closure/`，doc 17 的结果）列出 oob 函数、读写的字段、调用的 Linux 函数。再读代码确认。
2. **归类**：按截断规则（16 的规则 1–10、19 的 C1–C7、20 的句柄表）给每个函数、字段、调用定去向：信任基、可信、不可信（经门）、当输入校验。
3. **设计稿**：一个文件或一组文件一篇文档，写清门和数据划分。
4. **实施**：小步提交，每一步都不改变行为。
5. **验证**：全部测试对比基线；工具检查 T1–T3。

---

## 3. P0 的现状（2026-11-11）

| 项 | 状态 |
|---|---|
| 当前 HEAD 链接 | **已解决**：LKL blob 的 4 个数据段改名为 `.data`，14 个和内核边界符号重名的局部符号加上 `lkl` 前缀（doc 06 §4 已更正） |
| 当前 HEAD 启动 | 加 `nokaslr` 能启动；KASLR 下没有任何输出就退出。推测和 relocs 工具的警告有关，根因待查 |
| 测试 | 当前 HEAD（LKL + 未提交的 xbuf、通知中继改动）跑 libevl 全部 77 个测试：67 通过、10 失败，和 HEAD~1 基线完全相同 |
| 2026-11-11 更新 | initramfs 补上 `libm`、`libgcc_s`、`libatomic` 后，HEAD 的基线是 78 个测试 74 个通过，剩下 `ptrace-sync`（缺 gdb）和 3 个 `sched-*`（配置）。doc 24 的核心截断：74/78，与基线相同 |
| 待做 | 把构建、打包、QEMU、对比做成一个脚本（现在是手工命令，放在会话的临时目录里）；查清 KASLR；把 `tools/trust-closure` 改成能做验收；initramfs 补上 `libm`、`libgcc_s`，让 `fault`、`ring-spray`、`fpu-stress` 等测试能跑起来 |

10 个失败：`fault`、`ring-spray`（rc=127，缺共享库）；`fpu-stress`、`hectic`、`observable-race`、`thread-demote`（rc=134）；`ptrace-sync`（rc=137）；`sched-quota-accuracy`、`sched-tp-accuracy`、`sched-tp-overrun`（rc=42，配置里没开对应的调度类）。其中有几个可能只是测试环境的问题，P0 里要逐个查清，否则基线会掩盖真正的回归。

---

## 4. 未提交的改动怎么处理

> 2026-11-11 更新：第二版 xbuf 已被 doc 22 的最终设计取代，下面的建议仅作记录。

`linux-evl` 工作区里有 18 §16 的 xbuf 拆分和通知中继，还没有提交。建议：

- 通知中继（`inband_notify.c/h`、`init.c`）作为基础设施，在 P1 或 P4a 时单独提交；
- xbuf 的部分先保留在补丁里（`patches/2026-11-10-xbuf-split/`），工作区回退到干净状态，等 P7 再合入。这样前面各阶段的测试基线不受它影响。
