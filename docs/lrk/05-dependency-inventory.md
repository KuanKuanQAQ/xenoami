# EVL 对 Linux 的依赖盘点

## 方法

1. 另开构建目录 `build/linux-evl-full`，打开全部 EVL 选项（`EVL_NET`、`EVL_SCHED_QUOTA`、`EVL_SCHED_TP`、`EVL_DEBUG*`、`EVL_WATCHDOG`、`EVL_HIGH_PERCPU_CONCURRENCY` 等），只编译 `kernel/evl/` 和 `drivers/evl/`。
2. 对 56 个目标文件跑 `nm -u`，去掉 `evl_*` 自身符号。完整列表：`build/linux-evl-full/undef_all.txt`。
3. 对 helper，用 `objdump -dr` 找出是哪个函数发出的调用（用来定位藏在宏/内联函数里的调用）。

**局限**：`nm -u` 只能看到单独编译的函数和全局数据符号，**看不到内联函数、宏和结构体字段访问**。EVL 通过后者大量读写 task_struct、thread_info、mm、per-cpu 数据（见 [02](02-object-classification.md)）。完整盘点还要统计 EVL 访问了哪些 Linux 结构体字段（`git grep current/task_struct/thread_info/mm` 起步，彻底的话用 sparse 或 clang AST）。

内联 helper（`list_*`、`hash_add`、`jhash`、ktime 运算、cpumask）已编进 EVL，不产生链接依赖，换成调 LKL 反而多一次函数调用，不在迁移范围内。

## Helper A 类：直接交给 LKL（14 个，37 个调用点，已替换）

| 函数 | EVL 调用位置 | 路径 |
|---|---|---|
| `strcmp` | `factory.c:473`，`init.c:68` | inband |
| `strncmp` | `control.c:464/467` | inband（sysfs `state`）|
| `strncpy` | `thread.c:2617`（`thread_factory_build`）| inband |
| `strsep` | `clock.c:985`（`gravity_store`）| inband |
| `simple_strtol` | `clock.c:988`（同上）| inband |
| `hashlen_string` | `factory.c:468` | inband |
| `rb_insert_color` | `memory.c:193/211`，`timer.c:503`，`poll.c:165`，`observable.c:78`，`lib/map.c:49` | **oob** |
| `rb_erase` | `include/evl/timer.h:79`，`memory.c:223/225/253/261/264`，`observable.c:297/331`，`poll.c:308`，`lib/map.c:108` | **oob** |
| `rb_next` | `include/evl/timer.h:69`，`memory.c:134` | **oob** |
| `rb_replace_node` | `memory.c:266` | **oob** |
| `find_first_bit` | `include/evl/sched/queue.h:60`（只在 `CONFIG_EVL_SCHED_SCALABLE` 下编译；发出调用的是 `__evl_schedule`、`__evl_get_schedq`，调度器最热路径）| **oob** |
| `find_first_zero_bit` | `factory.c:76`，`sched/quota.c:522` | inband |
| `int_sqrt` | `drivers/evl/latmus.c:675` | inband |
| `ns_to_timespec64` | 经 `ktime_to_timespec64` 宏：`include/evl/clock.h:189`，`clock.c:513/521/537/544`，`syscall.c:237` | **oob**（`clock_gettime`）|

注意：
- `find_first_bit` 是内联函数，位图长度为不超过 64 的常量时直接 `__ffs` 展开；`EVL_MLQ_LEVELS` 超过 64，所以会调 `_find_first_bit`。
- `hashlen_string` 两边实现不同：宿主开了 `CONFIG_DCACHE_WORD_ACCESS`（按字长哈希），LKL 用逐字节通用实现。EVL 里只有 `factory.c:468` 一处计算哈希，前后一致，不影响正确性。
- `kstrtoull`（`control.c:508`）在 `CONFIG_EVL_SCHED_QUOTA` 里，默认关闭，已从清单去掉。

### 从 A 类去掉：`__sw_hweight64`

x86 上通过内联汇编调用（`arch/x86/include/asm/arch_hweight.h`）：

```c
asm (ALTERNATIVE("call __sw_hweight64", "popcntq %1, %0", X86_FEATURE_POPCNT) ...
```

函数名写在架构头文件的汇编字符串里，EVL 改不了；支持 popcnt 的 CPU 上启动时会被替换成 popcnt 指令，根本不调用。

## Helper B 类：是 L0，但有问题要注意

| 函数 | 位置 | 注意 |
|---|---|---|
| `snprintf`/`scnprintf` | 9 个文件，主要是 sysfs show | 取决于格式串，见 [03](03-function-levels.md) |
| `sort` | `latmus.c:708-741` | 带比较回调，LKL 代码反过来调 EVL 函数；IBT/CFI 打开时间接调用会出错 |
| `bitmap_parselist` | 经 `cpulist_parse`，`init.c:182`（启动参数 `evl.oobcpus`）| 只在启动时执行；位图长度 `nr_cpumask_bits` 两边不同 |
| `csum_partial`、`ip_send_check` | `net/ipv4/*`、`net/ethernet/input.c` | L0，但宿主是汇编优化版，LKL 是通用 C，网络路径性能下降 |

## Helper C 类：不适合交给 LKL

| 函数 | 原因 |
|---|---|
| `memcpy`/`memset`/`memmove` | 循环：LKL 的 `memcpy` 转调 `lkl_ops->memcpy`（`arch/lkl/include/asm/string.h:15`），而这个 host_op 由 EVL 用宿主 memcpy 实现；编译器在结构体赋值时也会自动生成调用 |
| `timespec64_to_jiffies` | `lib/uaccess.c:49`；LKL `HZ=100`、宿主 `HZ=1000`，结果差 10 倍 |
| `skb_put`/`skb_push`/`skb_pull_rcsum` 等 | `struct sk_buff` 布局随配置变化，两边不同 |

## 有状态依赖（选作例子的）

| 类型 | 例子 | 位置 | 说明 |
|---|---|---|---|
| inband 有状态 | Linux mutex `fac->hash_lock` | `factory.c:470-480`、`526`、`565`、`780`（`create_element_device` 等）| EVL 私有锁保护 EVL 私有哈希表，可复制；最典型的有状态函数（记录持有者 `current`、竞争时睡眠、等待队列）|
| oob 有状态 | `irq_work_queue`（proxy 写路径）| `proxy.c:223` → `work.c:101` | 最典型的 oob 有状态依赖，见 [09](09-case-oob-irq-work.md) |
| oob 有状态 | `ktime_get_mono_fast_ns` | `include/evl/clock.h:83`；调用者 `clock.c:1076`、`sched/quota.c:375/471`、`sched/tp.c:47/267`、`thread.c:853/911` | 作为分析和讨论材料，见 [08](08-open-questions.md) |

反面例子（不做）：`proxy.c` 的 `worker_lock` 与 Linux VFS 的 `filp->f_pos_lock` 嵌套（第 83、90 行），换成 LKL 锁会出现跨两个内核的锁顺序问题。

## oob 上通过函数调用访问的有状态依赖

从 `nm -u` 看，几乎找不到"可复制"的：EVL 已把 oob 用的分配器、锁、等待队列换成自己的实现。剩下的是唯一资源：
- **时间**：`ktime_get_mono_fast_ns`
- **跨 stage 通知**：`irq_work_queue`（16 个目标文件），经 `evl_call_inband_from`（`work.c:101`）
- **用户地址空间**：`raw_copy_from_user` 等
- **硬锁慢路径**：`queued_spin_lock_slowpath`（Linux 的 qspinlock 代码，用 per-cpu MCS 节点）

但通过**数据访问**（内联、宏、字段）的依赖很多，见上面"局限"。
