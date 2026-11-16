# 进度日志

## 2026-10-26

- 镜像仓库到 GitHub（KuanKuanQAQ/lkl、linux-evl、libevl）。lkl/linux 不能直接 fork（`KuanKuanQAQ/CRR` 已占 torvalds/linux 的 fork 网络）；Xenomai 4 不在 GitHub。都建成独立公开仓库，分批推送（GitHub 单次 2GB 上限）。
- 确定目标和三条原则，提出对象三分类。

## 2026-10-27

- 三个仓库编译完成，生成 `compile_commands.json` 和 `.clangd`。
- 讨论函数分级（L0/L1/L2）、调用 LKL 的开销、单核影响、实时性规则。
- 依赖盘点：全选项构建 `build/linux-evl-full`，`nm -u` 得到外部符号列表；helper 分 A/B/C 三类，A 类 14 个。
- 讨论 `tk_fast_mono` 的 latch 协议、irq_work 的归类、task_struct 依赖。
- 实现 A 类迁移：
  - [x] LKL 裁剪配置（关 PCI/NET/BLOCK/VIRTIO，需连带关 `VIRTIO_MMIO`）
  - [x] `arch/lkl/kernel/evl_glue.c`（14 个胶水函数）、`arch/lkl/kernel/Makefile`、`LKL_ENTRY_POINTS`
  - [x] `arch/lkl/Makefile` 加 `LKL_KABI=y` 开关（KCFLAGS 方案因 kbuild 重复 include 失败）
  - [x] 内核 ABI 编译 `lkl.o`：`nm -u` 只剩 `lkl_bug`、`lkl_printf`
  - [x] objcopy 段重命名（`.notes` 不能删，改为重命名；补上 `.pci_fixup`）
  - [x] `kernel/evl/Makefile`、`kernel/evl/lkl_host.c`、`include/evl/lkl.h`
  - [x] 替换 37 处调用点，`nm -u` 检查（默认配置和全选项配置都干净；`kernel/evl/net` 不在范围内）
  - [x] 宿主内核 QEMU 配置（`build/linux-evl-qemu`）
  - [ ] 编译 bzImage（链接 vmlinux）
  - [ ] initramfs + QEMU 测试
- oob 有状态依赖案例：选 `irq_work_queue`（proxy 写路径），梳理三种执行环境、两次接力、三个信箱，写成 [09](09-case-oob-irq-work.md) 和 `figs/oob-irq-work.svg`。

## 2026-10-28

- helper 修改提交并推送：lkl `evl-glue` b89db653a79b，linux-evl `evl-lkl` daed79c1cd9b（`lkl_blob.o_shipped` 107MB 超 GitHub 上限，不入库，本地 exclude）。
- 写 [10](10-irq-work-glossary.md)：irq_work 路径名词表。
- 画 `figs/irq-work-fields.svg`：29 个函数按 EVL/Linux/Dovetail 着色，15 个结构体全部字段，函数↔字段读写双向标注。

## 2026-11-02

- 通读 proxy.c：输出/输入两个方向、预留-放锁拷贝-统一发布、只在空变非空时触发 relay、inband 写者同步 relay、oob/inband 两套等待对象。
- 写 [11](11-proxy-linux-deps.md)：proxy.c 的 Linux 依赖（`nm -u` + `objdump -dr` 按函数归并 + 内联展开），Linux 函数读写的 EVL 对象，反方向回调；讨论 EVL/Linux 域与跨域门的定义，指出内联代码、Linux 分配器分配的 EVL 对象、嵌入的 Linux 原语、指针参数、栈和 per-CPU 区等难以归类的情形。
- 附带发现（未验证）：`proxy_oob_poll` 传错 `filp`；EVL oob 路径无 `access_ok`；`relay_output` 出错后可能卡死。
- 写 [12](12-domain-partitioning.md)：结合 SoK 讨论域划分。结论：以代码为中心（E / L / D 三个静态代码域）+ stage 约束的混合模型；数据按访问者和同步规则分 E-oob / E-ib / 边界 / L；三条规则（oob 只能用 E∪D、inband 可跨域、跨 E-oob 和 L 的逻辑必须拆分）；proxy 输出路径逐步走查。否定"每个 EVL 线程一个 LKL"（以数据为中心）的方案。

## 2026-11-03

- 修订 [12](12-domain-partitioning.md)：R1 由"oob 不能调 Linux"改为"oob 只能调用对 oob 安全的代码"；新增 §4.4 解释"对 oob 安全"的三个条件，以 `queue_work` 死锁时间线、`mutex_lock`、`irq_work_queue`（NMI 安全 + Dovetail 改造退出路径）、`queued_spin_lock_slowpath`（4 层嵌套节点）为例；R2 补充"持有 oob 也用的锁时不能调 Linux"。
- 写 [13](13-oob-shared-state.md)：两天讨论的完整梳理。LKL 约束（无硬件页表、单核、单例、两级调度）；否定"每线程一个 LKL"；以代码为中心 + stage 约束；只在 inband 运行的函数不管、stage 分支抽成函数；oob 任意打断 inband → 对 oob 安全的三个条件、Dovetail 补丁（fget/fput/printk）；"与 Linux 共享的状态"取代"全局状态"，字段组为单位，六种协议；hard lock 与 EVL 的锁；proxy/work 逐一分析；无状态函数需不依赖 LKL 单核原语；共享状态按快照 / 镜像 / 另建或共享区处理，共享区是必须信任的最小集合而非保护机制；f_op 快照与 O_NONBLOCK 镜像两个试验方案。
- 按 13 §11 改造 linux-evl（未提交），记录在 [impl-shared-state/](impl-shared-state/)：① proxy 目标文件 `f_op->oob_poll` 改为创建时快照；② `O_NONBLOCK` 镜像到 `evl_file.nonblock`，Linux 在 `setfl` / `ioctl_fionbio` 的 `f_lock` 内调用新 Dovetail 弱钩子 `handle_inband_fflags_change` 推送；③ `evl_work` 不再借用 Linux irq_work，改用 EVL 自己的 per-CPU 链表 + Dovetail 合成中断（`evl_relay_sirq`），协议照搬 irq_work。两种配置编译通过；objdump 确认 proxy oob 函数不再读 `f_op` / `f_flags`。运行测试未做。发现工作区 `kernel/evl/syscall.c` 第 1 行有一处非本次的误改（`++/*`）。

## 2026-11-04

- 讨论 `proxy_oob_poll` 第 4 步（转发到目标文件的 `oob_poll`）：proxy 的 oob 读写只看 ring，数据总由 inband `kernel_read/write` 转发；`kernel_read/write` 要求 `read_iter/write_iter`，EVL 设备用 `read/write`，proxy 包不了 EVL 设备；真正会走到这一步的是 socket（`CONFIG_EVL_NET` 下所有 socket 带 `sock_oob_poll`），原代码传 proxy 自己的 `filp`，`sock_oob_poll` 会把 proxy 的 `evl_file_binding` 当成 `evl_socket` 调用乱指的函数指针。读代码确认，未运行复现。
- 修改 linux-evl：删除 `proxy_oob_poll` 的转发，去掉 `target_oob_poll` 快照。重写 [impl-shared-state/01](impl-shared-state/01-drop-oob-poll-forward.md)（旧的 `01-fop-snapshot.md` 删除，快照方案作为被替代方案记在新文档 §7），更新 README、02 的行号、补丁；更新 11 §5、13 §12/§14。两种配置编译通过，`proxy_oob_poll` 不再有间接调用。
- 更正：inband 版 `proxy_poll` 转发目标 `poll` 是有道理的（inband 的 `proxy_read` / `proxy_write` 会在调用者上下文里同步调 `relay_input` / `relay_output` 访问目标；inband 版不做预读，要靠目标等待队列叫醒 poller）。oob 版用预读代替转发。已更正 impl-shared-state/01 §2.4、§3 和 README。

## 2026-11-05

- 写 [impl-shared-state/04](impl-shared-state/04-proxy-bridge-design.md) 设计稿（未实施）：`relay_output` / `relay_input` 及 inband 入口划到非可信一侧；`proxy_ring` 拆成 EVL 侧、Linux 侧（`proxy_bridge`）和共享数据区；13 个门（消费、发布、通知、ring 读写等），第一阶段只报告不拒绝；`do_proxy_write` 的 `running_inband()` 分支消失，改由调用者按 stage 触发转发；新建 `proxy_bridge.c` 让编译器保证边界。`worker_lock`、`f_pos_lock` 都是 inband 锁，oob 锁只有 `ring->lock`。读代码新发现（未验证）：P2 `relay_output` 出错/写 0 时 `rdoff` 落后于消费量；P4 `relay_input` 短读时 `wroff` 前进 `n` 而 `fillsz` 只加 `ret`；P5 `reqsz` 可能超过剩余空间导致覆盖。
- 按 04 实现 proxy 的 EVL / Linux 拆分，逐处说明写在 [impl-shared-state/05](impl-shared-state/05-proxy-bridge-impl.md)：新文件 `kernel/evl/proxy_bridge.c`（两个转发者 + 4 个 inband 文件操作）、`kernel/evl/proxy_gate.h`；15 个 `proxy_gate_*` 门；`proxy_ring` 去掉 `worker_lock`、两个 inband 等待队列和转发者的下标（`wroff`/`rdoff` 改为 union）。两种配置 `W=1` 编译通过；`proxy_bridge.o` 不引用任何 `evl_*` 符号；在 bridge 里直接访问 `evl_proxy` 会编译失败。行为不变，P1–P6 都保留。发现 04 的一个漏洞：非 EVL 线程调 `oob_write` 时 `proxy_oob_write` 跑在 inband 上（`evl_switch_oob` 返回 `-EPERM` 后照常执行），所以 oob 入口保留了一个只有一行调用的 `running_inband()` 分支。未跑 QEMU。

## 2026-11-06

- 写 [14-trust-domain-vs-stage.md](14-trust-domain-vs-stage.md)，合并 11-05、11-06 两轮讨论。核心结论：stage 决定执行的保证，域决定内存的保护；可信域 = oob 执行的 TCB，oob ⇒ 可信，反之不成立；写可信对象的 inband 代码（工厂、`.release`）也是可信代码，只需"不写错"、不需"按时完成"。对象按"oob 是否依赖它的值"归属；函数在 oob 上必须可信，在 inband 上看是否不经门写可信对象。可信代码在 inband 上可以同步调用不可信代码（不持 oob 会争用的锁），因此上一轮认为违规的 `relay_output_work`、`proxy_oob_write` inband 分支不违规；仍违规的是所有入口的 `element_of(filp)`（用了不可信的控制信息，未校验）。新疑点 P7：`proxy_release` 踢醒同一 element 所有 fd 上的 oob 等待者。在 doc 12（C12-1～10）、doc 13（C13-1～9）、impl 04、05 插入修正标记，原文未改。

## 2026-11-09

- 按"oob 读的字段 → 写它们的函数及其依赖 → 截断"的流程，用 clang JSON AST 做了第一次自动分析，写成 [15-trust-closure-pilot.md](15-trust-closure-pilot.md)，工具在 `tools/trust-closure/`。范围：proxy.c、proxy_bridge.c、work.c、file.c、factory.c、syscall.c（HEAD 版）。结果：23 个 oob 函数、45 个可信字段（第 1 步 36，第 2 步 9）、9 类截断点；函数归属 A/B/C/D = 17/36/8/47。对原始 proxy.c 跑同一套分析，`relay_output/input`、`proxy_write/read` 被判为可信，`kernel_write/read` 的返回值流进 `fillsz`、`reqsz` 等，现在的代码里换成了 18 处"门的输入"，复现了 01/02/05。新发现（未验证）：oob 系统调用经 Linux 对象里的 `f_op->oob_read` 分派；`evl_open_element` 由 `inode->i_cdev` 决定绑定哪个 element；fd → element 整条链经过三个 Linux 侧指针；`__evl_put_element` 在 oob 上仍调用 Linux 的 `irq_work_queue`；`relay_*_work` 回调应为不可信（已更正 doc 14 §10.1）。
- 工作区回退到原始代码。11-03 至 11-05 的修改（01/02/03/05）存为补丁并写了 README：`~/xenoami/patches/2026-11-09-shared-state-wip/`，已用 `git apply --check` 确认能重新打上；`syscall.c` 第 1 行的 `++/*` 单独存为 `syscall-stray-edit.patch`。impl-shared-state 和 doc 15 开头加了说明（doc 15 §5.2/§7 预设门是循环论证）。
- 分析工具改为按配置运行（`cfg/*.json`，默认不预设门，新增"与用户态共享的内存"类别），对 sem、xbuf、monitor、observable 做了闭包分析，并逐个写了功能、原理、字段、函数语义：[16-subsystems/](16-subsystems/README.md)。结论：三个子系统都不调用大型 Linux 函数，和 Linux 的交集是同样的几种模式反复出现（偷看 Linux 等待队列、oob 上的 irq_work 跳板及其返回值影响引用计数、`O_NONBLOCK`、`element_of`、Dovetail 的 per-task/per-mm 状态、分配器），应在 EVL 核心层面统一处理；proxy 是特例。待验证：EVL 共享堆被整体映射给任意调用 mmap 的进程，非实时进程可能改写实时任务的 sstate；`evl_get_xbuf` 经 latmus 的 oob ioctl 在 oob 上读 `f_op`；monitor 的 `inband_wake_w` 是死代码。
- 讨论"能否完备地确定读集合和写集合"：精确不可能，只能保守地不漏；写集合在 Linux 里静态上做不到，靠内存隔离兜底；漏掉的读隔离兜不住，比漏掉的写更危险。
- 按"调用者必须可信、传值由调用者负责、传指针要找全写者、以分配单元的类型为单位"重写 [16-subsystems/](16-subsystems/README.md) 第二版：README 新增划分规则 1–10（每条带例子）和按分配单元的总表，四篇的第 5、6 节按规则重写，第 1–4 节不变。类型级结论是人工推导的。主要结论：`evl_element` 本身嵌着 `rcu_head`、`cdev`、`irq_work`，所有 EVL 对象都要拆分；M1/M2 实质是"可信对象嵌入了 Linux 写的成员"；inband fops、irq_work 回调、退出钩子是"Linux 入口拿不可信指针写可信状态"；`oob_thread_state`（含 `evl_current()` 的来源）是共性问题；xbuf 的 `bufmem` 可以放在不可信域。**更正**：第一版"sem 在树内没有调用者"是错的，`drivers/spi/spidev.c` 把 `evl_ksem` 嵌在 `spidev_data.oob` 里使用，`evl_ksem` 从来不是独立的分配单元。

## 2026-11-10

- 全量划分，写 [17-whole-evl-annotation.md](17-whole-evl-annotation.md)。工具新增 `extract_all.py`（结构体、typedef、全局和栈上对象、分配点、存进字段的函数、函数参数）、`analyze_all.py`（oob 根自动识别、经字段和签名解析间接调用、inband 证据、函数内 stage 切换、数据闭包、按分配单元放置、可信代码的上下双向传播）、`annotate.py`（行尾追加 `// @TD` 注释，可清除）、`check_syntax.sh`。在 `linux-evl` 工作区的 86 个文件、1580 行追加了注释，逐行确认只在行尾追加，32 个编译单元语法检查通过；未提交，补丁副本在 `~/xenoami/patches/2026-11-10-trust-annotations/`。结果：855 个函数中确定 oob 445、可能 oob 87、可信 816；141 个类型中可信 82、用户输入 39、不可信 13、用户共享 7；规则 8 成员 37 个（含 3 个 Linux 锁）；由 Linux 调用的可信函数 67 个。新发现（未验证）：oob 系统调用经 Linux 的 fd 表（`CLASS(fd)`）找文件；oob 系统调用的权限由 `task_struct.cred` 决定；tick 在 oob 上调用 Linux 时钟设备驱动的 `set_next_event`；`monitor_oob_write` 在 oob 上用非 raw 的 `copy_from_user`。
- 写 [18-xbuf-split.md](18-xbuf-split.md) 第一版：可信核心（reserve/commit）、两个 stage 的读写路径、10 个门、不变式 I1–I5、通知中继。
- 讨论后改变思路：xbuf 和 proxy 的对端不可信，写进来的内容本来就不可信，所以不保护通道的正确性，只保护可信内存、实时性和可信的控制流。写 [19-untrusted-peer-channels.md](19-untrusted-peer-channels.md)：规则 C1–C7，并用 proxy 逐字段、逐函数对照，规则成立。proxy 多出三种情形：
  - 同一端有两个域的调用者：inband VFS 入口经门，由可信代码一次完成整次操作；
  - `on_error` 原样返回给 oob 调用者：要约束到负的 errno；
  - `proxy_oob_poll` 经目标文件的 `f_op` 转发：删除。
  原来记的转发者 bug（04 的 P2、P4、P5）不再影响划分。
- 按 19 重写 18（第二版）：环按端拆开，`fillsz` 改成共享的原子计数，可信端用 `fill_of` 约束后使用；门铃 `xbuf_gate_kick`；通知中继做成公共设施 `evl_inband_notify`。门减到 3 个。
- 门铃不做去重：去重挡不住风暴，而且会丢唤醒（推导见 18 §7.2）。跨 CPU 的唤醒风暴记为 19 U1。
- 用户从工作区回退了 doc 17 的 `// @TD` 注释，补丁副本仍在 `patches/2026-11-10-trust-annotations/`。
- 按 18 第二版实施步骤 1–3（未提交，补丁副本在 `patches/2026-11-10-xbuf-split/`）：新增公共的通知中继 `kernel/evl/inband_notify.c`（合成中断 + per-CPU llist，处理函数按类型分派）；`xbuf.c` 按端拆分，`fillsz` 改成共享原子计数并用 `fill_of` 约束，加门铃 `xbuf_gate_kick`，删除 6 个回调和 `irq_work`。当前 HEAD 因 `lkl_blob.o` 的孤儿段链接失败（和本改动无关），所以在 HEAD~1 上打同一个补丁做测试：libevl 77 个测试 67 通过、10 失败，和不打补丁的基线完全相同；xbuf、proxy、poll 测试全部通过，新中继的合成中断确实被触发。和设计的差别见 18 §16。
- 分析 factory.c，写 [20-factory-and-handles.md](20-factory-and-handles.md)。oob 系统调用经 Linux fd 表、`f_op`、`private_data` 三个 Linux 侧指针找对象。用户决定不用"Linux 查到再校验"（挡不住换成另一个合法对象），改为在可信域维护 per-process 句柄表，句柄只由可信代码创建（clone 时返回；公开 element 按名字 attach），libevl 改 ABI。读代码新发现（未验证）：B1 flusher kthread 用错 `task_struct`；B2 `evl_create_core_device` 对 `getname_kernel` 的错误检查写错。

## 2026-11-11

- 用户调整方向：先做 EVL 的基础（中断、内存、调度、锁、thread、factory），xbuf、proxy 放到后面。写 [21-roadmap.md](21-roadmap.md)。
- P0：**当前 HEAD（接入 LKL）第一次链接成功**。两个问题：
  - LKL 的 `.data.lkl_*` 段在 x86 默认配置下不会被收进 `.data`，改名为 `.data`；
  - LKL 的局部符号 `__per_cpu_start` 等和内核的边界符号重名，导致 kallsyms 失败，用 `--redefine-syms` 加上 `lkl` 前缀。
  
  doc 06 §4 已更正。KASLR 下启动时没有任何输出，加 `nokaslr` 才能启动，根因待查。当前 HEAD 加上未提交的 xbuf 改动跑 libevl 全部测试：67 通过、10 失败，和 HEAD~1 基线相同。

## 2026-11-11（续）xbuf、proxy 最终设计

- 写成 doc 22（可以单独阅读）。分析方法改为：按功能流程列出标记 → 谁写谁读 → 代入 Z/z → 交集分黄色（Z 受 z 影响）和紫色（Z 要通知 z）→ 截断。截断方法共五种：私有副本、输入化、只记录不调用、归属单侧、消除。
- 丢弃了未提交的第二版（共享 `fillsz`），从 HEAD 重新实现：
  - 每个环拆成可信端、不可信端、共享部分；
  - 可信端自己保存 `fill′`，z 只经门报告数量，可信端截断后更新；
  - Z → z 只记录（通知中继，带 BUSY 位和 drain）；
  - proxy 去掉 inband 的 `read()`/`write()`，libevl 同步修改；
  - proxy 叫醒转发者的记录带 element 引用；
  - 顺带修了 proxy 转发者的下标错位和请求越界两个问题。
- 测试：libevl 67 个通过、10 个失败，和基线相同；新增的 `tools/pipe-stress` 全部通过。
- 改动尚未提交。

## 2026-11-11（续）EVL 核心的截断

- 用户同意 proxy 阻塞写保持现状（打印线程方案和阻塞写本质相同：缓冲有界，满了只能拖住生产者或丢弃）。
- 用户授权自主选择、修改更多 EVL 核心文件。写 [24-core-cut.md](24-core-cut.md)，并实现：
  - 盘点 EVL 核心的 9 类 Z → z 通知，全部改为经通知中继只记录，`kernel/evl` 里不再有 `irq_work`；
  - 中继第二版：新增 `WAKE_ALL`、`WAKE_TASK`、`COMPLETE`、`CALL` 类型，合并位和值，一次性记录，inband 上 post 时同步投递；生命周期改为 dispose 时 drain，不再每次通知都 get/put element；
  - monitor、observable 的 Linux 等待队列移到单独分配的 inband 结构；
  - oob 信号改为每线程一条固定记录（原来每次从可信堆分配，z 不投递就能耗尽可信堆）；
  - oob 系统调用不再读 Linux 凭据；`clock_sleep` 的重启日期存在可信线程里；monitor 的 oob 写去掉 `might_fault`；
  - 修 doc 20 的 B1–B3。
- P0：initramfs 补上 `libm`、`libgcc_s`、`libatomic`，原来因环境失败的 6 个测试可以运行。用同一个 initramfs 重跑未改动的 HEAD 作为新基线：78 个测试 74 个通过（ptrace-sync 缺 gdb，3 个 sched-* 是配置问题）。改动后同样是 74/78，失败相同，没有 WARNING。
- 改动尚未提交。

## 2026-11-12 monitor、observable 文档

- 建立原版代码的只读 worktree：`~/xenoami/linux-evl-orig`（上游 `v6.12.y-cip-evl-rebase`，`5f5548b11fbd`），方便对照。
- 写 [25-monitor-observable.md](25-monitor-observable.md)：两个组件的功能、交集 X1–X11、修改和不变式。新发现：inband 等待路径（`__wait_monitor_inband`、`pull_from_inband`）先拿 Linux 锁再拿可信硬锁，是混合代码；建议改为"门 + 标准等待写法"，作为下一步。
- 写 [26-zz-method.md](26-zz-method.md)：从安全目的出发，结合例子总结 Z/z 的思想和方法，可单独阅读。
- 写 [27-inband-notify.md](27-inband-notify.md)：通知机制单独成文。核对了原版全部 `irq_work` 调用点；确认中继的合并语义与 irq_work 相同；记下 SIGDEBUG 原因码由"保留第一个"变为"保留最后一个"的差异；锁嵌套问题 stax 的 `claim_stax_from_inband` 也有（doc 25 只列了 monitor、observable）。

## 2026-11-12 通知机制文档、thread.c

- 写 [27-inband-notify.md](27-inband-notify.md) 后，又讨论了 irq_work 的 claim、PENDING/BUSY、P2 内核线程启动等细节（问答，未入文档）。
- 写 [28-thread.md](28-thread.md) 并实现：sysfs 7 个读接口改为"Z 门返回值、z 格式化"；`pin_to_initial_cpu` 约束 `task_cpu()`；`evl_killall` 的 printk 移出 `thread_list_lock`。回归 74/78 与基线相同；sysfs 并发读取 4 万多次无告警。
- 待用户决定：z 发起的控制请求（inband ioctl 改调度、demote、suspend，killall）是否放行（doc 28 §5.2 三个方案）；原版 bug B4（GET_STATE 的 state 填成 CPU 号）是否修。
- 改动尚未提交。

## 2026-11-13 通知中继收尾、EVL 线程控制权限

- 问答：irq_work 处理函数仍在关中断下运行，它解决的是"不能拿锁"而不是"关中断"；需要睡眠的事再经 workqueue 跳一次（`evl_call_inband`），这一跳在 z 侧，不用改。
- 通知中继：monitor、observable 去掉 `waitqueue_active()`，无条件 post，Z 不再读 z 的等待队列；take 闸门复制 `fn`；done 闸门不带参数，Z 用 per-CPU `evl_notify_taken` 记录正在投递的记录，z 手里不再有 Z 的地址。写 [29-notify-relay.md](29-notify-relay.md)，doc 27 加了指向 29 的说明。
- 用户确定安全假设：EVL 线程（实时任务）都可信，其他任务不可信；EVL 线程之间互相控制允许，普通线程不能影响 EVL 线程的运行。即 doc 28 §5.2 方案 ②。实现 `evl_may_control()`，改 8 处入口，写 [30-control-policy.md](30-control-policy.md)。
- libevl：`tests/sched-tp-accuracy.c` 的 main 在安装 TP 调度表前先 attach（新规则下非 EVL 线程不能改调度表）。
- 测试：QEMU 回归 74/78，与基线相同；权限测试（普通线程 / EVL 线程各一轮）全部符合预期；另编打开 TP/QUOTA 的内核，`sched-tp-accuracy`、`sched-tp-overrun` 3/3 通过，`sched-quota-accuracy` 精度 108% 左右失败（与权限无关，未做基线对照）。
- 待定：谁能 attach（信任边界）；Linux 自己的信号、ptrace、亲和性等操作 EVL 拦不住；`latmus -r` 需要 libevl 先 attach 再 reset；doc 28 的 B4、B5。
- 改动尚未提交。

## 2026-11-16 交接收尾

- 上述内核改动已整理为四个提交：`2495805ce5b4`（通知中继）、`789a2167180f`（xbuf / proxy 拆分）、`7b70298bd41d`（线程路径截断）、`458f163d4386`（控制权限）；libevl 配套提交为 `2f2bc11e694f`（proxy 用户接口）、`07eeff23e0b8`（TP 测试先 attach）。六个提交均已推送到各自 GitHub 远端，沿用既有的内核 `evl-lkl` 和 libevl `master` 分支。
- 补齐 [逐提交说明与配套索引](commits/README.md)，核对通知中继说明中的指针、生命周期和测试配置表述；收录 doc 28–30 及 [接手理解报告](31-project-handover-2026-11-16.md)。历史日志中的“尚未提交”描述当时状态，以本条记录为后续进展。
- 本次父仓库提交将 `linux-evl` 指针从 `daed79c1cd9b` 更新到 `458f163d4386`，`libevl` 从 `db33f300adcf` 更新到 `07eeff23e0b8`；原版对照内核和 LKL 指针保持不变。libevl 工作区 `.gitignore` 的本地 `.cache` 条目保留，不包含在这些提交中。
- 本次没有修改实现，也没有重跑构建或 QEMU 测试；74/78、TP 和 quota 的结果均为历史记录。交接检查包括文档相对链接、代码围栏、差异空白检查、提交顺序及配套版本核对。安全隔离、可信句柄表及既有待办仍未完成。
