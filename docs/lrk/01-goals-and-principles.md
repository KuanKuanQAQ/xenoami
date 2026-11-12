# 目标与原则

## 目标

EVL 核心现在直接调用宿主 Linux 的大量函数。目标是把这些依赖改为调用 LKL（Linux Kernel Library，作为库运行的 Linux 内核）提供的函数，LKL 运行所需的 host_ops 由 EVL 提供。

## 三条原则（按重要性排序）

1. **不能破坏 EVL 的实时性。** 最重要，违反即否决。LKL 本身完全不考虑实时性，需要在这方面做修改或加约束。
2. **EVL 尽量小。** 为了实时性和可信基（TCB）。希望引入 LKL 能帮助 EVL 变小。
3. **对 Linux、EVL、LKL 的改动尽量少**，或者用独立的代码实现所需功能。重要性最低，做不到时可以放弃。

## 整体架构设想

```
        ┌───────────── 宿主内核地址空间 ─────────────┐
        │                                            │
  Linux (inband) ── Dovetail ── EVL core (oob) ── lkl.o
        │            中断流水线    │   ▲  host_ops   │
        │                         └───┘              │
        └────────────────────────────────────────────┘
```

- Dovetail 负责中断流水线和 inband/oob 两个 stage 的切换，属于硬件层，留在宿主 Linux 里。
- EVL 掌管实时调度、定时器和 oob 路径。
- LKL 以一个预编译的 `lkl.o` 链接进 EVL（`kernel/evl/`），作为"第二份 Linux"给 EVL 提供内核服务；它运行需要的线程、锁、定时器、内存由 EVL 通过 host_ops 提供。

最终形态有点像：EVL 掌管硬件，宿主 Linux 像一个半虚拟化客户机，LKL 是给 EVL 用的内核服务库。可参考的相关工作：Genode/L4 的 DDE、NetBSD rump kernel（anykernel）、L4Linux。

## LKL 的关键特性（已从代码确认）

- **单 CPU**，用一把全局 CPU 锁（`lkl_cpu_get/put`，`arch/lkl/kernel/cpu.c`）串行化所有 LKL 内部执行。
- **"关中断"只是软件变量** `irqs_enabled`（`arch/lkl/kernel/irq.c:209-215`），不碰硬件中断。LKL 线程如果是低优先级 EVL 线程，它的代码任何位置都能被更高优先级的 EVL 线程抢占。
- **符号隔离**：`lkl.o` 生成时 objcopy `-G` 只保留 `LKL_ENTRY_POINTS` 里的符号为全局，其余全部变局部，LKL 的 `kmalloc` 等不会和宿主同名符号冲突（`arch/lkl/Makefile`）。
- **一个二进制里只能有一个 LKL 实例**：它的状态都是全局变量。
- 上游 LKL 目前没有 SMP 支持（未完全确认）。
