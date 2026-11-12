# 函数分级：L0 / L1 / L2

"有状态还是无状态"只是近似说法。真正要判断的是：**这个函数是否依赖 LKL 的执行环境**。

## 分级

| 级别 | 含义 | 调用方式 |
|---|---|---|
| **L0 纯函数** | 函数及其调用的所有函数，只碰参数、调用方的内存、栈和只读常量 | 直接调用，可以出现在 oob 实时路径上；LKL 甚至不需要已经启动 |
| **L1 不睡眠但碰 LKL 状态** | 需要持有 LKL 的 CPU 锁，`current` 要是正确的任务 | 走 `lkl_evl_call` |
| **L2 可能睡眠** | 必须作为一个正常的 LKL 任务运行 | 走 `lkl_evl_call` |

实际操作上 L1 和 L2 走同一条路，所以二分法可以保留，判定标准是"是不是 L0"。**只有 L0 函数允许出现在实时路径上。**

## 判定标准

对函数**及其调用闭包**检查，满足任意一条就不是 L0：

1. **读写可变的全局状态**：普通全局变量、静态变量、per-cpu 变量。只读也算（可能读到改了一半的数据）。
   只读常量没问题：`const` 表（如 `_ctype[]`）、`.rodata`、`__ro_after_init`。
2. **隐式的全局状态**（最容易漏，宏展开后才看得到）：
   - `current` / `current_thread_info()`：LKL 里是全局变量 `_current_thread_info`（`arch/lkl/include/asm/thread_info.h:48`）
   - `preempt_count`：`spin_lock`、`preempt_disable`、`rcu_read_lock` 都会改
   - 中断标志：`local_irq_save/restore` 读写 `irqs_enabled`，restore 时还可能执行 `run_irqs()`（`arch/lkl/kernel/irq.c:211`）
   - 内存分配器、printk/WARN、RCU、tracing 等调试钩子；**注意出错路径**
3. **可能睡眠或调度**（可能调到 `schedule()`）。
4. **有间接调用**（函数指针、回调）：静态无法确认，按有依赖处理。

**跨内核调用还要额外满足两条**（在 helper 盘点中发现）：

5. **配置常量两边一致**：`HZ`、`PAGE_SIZE`、`NR_CPUS`、`nr_cpumask_bits` 等会编译进函数体。例：LKL `CONFIG_HZ=100`，宿主 `1000`，`timespec64_to_jiffies` 交给 LKL 算会差 10 倍且不报错。
6. **参数类型的二进制布局两边一致**：`rb_node`、`timespec64`、字符串、`unsigned long` 位图可以；`sk_buff`、`task_struct` 等随配置变化的结构体不行。

## 反面例子：LKL 的原子位操作不是 L0

LKL 是 `CONFIG_SMP=n`，`arch/lkl/include/asm/` 没有自己的原子操作，用通用实现。非 SMP 下靠"关中断"保证原子性（`include/asm-generic/atomic.h:57` 起）：

```c
raw_local_irq_save(flags);    // 读写 LKL 全局变量 irqs_enabled
v->counter = v->counter c_op i;
raw_local_irq_restore(flags); // 可能直接执行 run_irqs()
```

后果：
1. 不是真正的原子操作——另一个 CPU 上的 EVL 调用方照样会冲突；
2. 从 LKL 外部调用会并发修改 LKL 的 `irqs_enabled`，还可能在调用方线程上不持锁执行 LKL 的中断处理。

所以 `test_and_set_bit`、`clear_bit` 等**不能**当 L0 直接调用。

## 另一个反面例子：`snprintf` 看起来像纯函数

`%p` 要用全局哈希密钥；`%pS` 查符号表（交给 LKL 查到的是 LKL 自己的符号）。能否算 L0 取决于格式串。EVL 核心用到的只有 `%d %s %Lu %u %zu`，可以算 L0；`kernel/evl/net` 用了 `%pI4`，要逐条确认。

## 判定方法

1. **看预处理后的源码**：`make ARCH=lkl O=<dir> lib/kstrtox.i`，宏全部展开，`current`、`irq_save`、`preempt` 都能看到。
2. **用重定位做机械检查**（对 objcopy 之前的 LKL `vmlinux`）：从目标函数出发沿 `objdump -dr` 的 `call` 重定位展开调用图，遇到以下情况标记为非 L0：
   - 引用可写段（`.data`、`.bss`，`.data..ro_after_init` 除外）
   - 调用已知环境函数（`arch_local_irq_restore`、`schedule`、`__might_sleep`、`_printk`、`__kmalloc*`）
   - 间接调用

   可写成脚本，以后迁移更多函数时复用。
3. **运行时自检**：调试构建时在每个 L0 胶水函数前后比较 LKL 的 `irqs_enabled` 和 `_current_thread_info->preempt_count`，有变化就报警。

## 一点观察

L0 函数其实不需要 LKL 提供"环境"，LKL 对它们的作用只是"一份不依赖宿主 Linux 的副本"。LKL 真正发挥作用的是 L1/L2 这类需要内核环境的函数。
