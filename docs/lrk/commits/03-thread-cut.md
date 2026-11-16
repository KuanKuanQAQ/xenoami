# commit 03：线程路径中的不可信状态依赖

内核提交 [`7b70298bd41d`](https://github.com/KuanKuanQAQ/linux-evl/commit/7b70298bd41d4b6434f42161fc5600df96cf1142)，父提交 `789a2167180f`。涉及 `thread.c`、`syscall.c`、`clock.c` 和 `include/evl/thread.h`。

线程代码除调度与等待外，还承担 attach、Linux 任务事件、sysfs 查询和系统调用入口。本提交处理其中五类依赖；“谁可以控制线程”的授权策略属于后续 [commit 04](04-control-policy.md)。

| 路径 | 原来的依赖 | 修改后 |
|---|---|---|
| oob 系统调用入口 | 每次读取 Linux 的 CAP_SYS_NICE 凭据 | 只检查当前线程已 attach，即 `evl_current()` 非空 |
| 时钟睡眠重启 | 从 Linux restart_block 取回截止日期 | 截止日期保存到可信线程的 `restart_expires` |
| 初始 CPU 选择 | 直接用 `task_cpu()` 检查位图和索引运行队列 | 先检查 `nr_cpu_ids` 上界和可信亲和性掩码 |
| 批量取消线程 | 持 `thread_list_lock` 调 printk | 先取对象引用、放锁，再打印和取消 |
| sysfs 查询 | 持可信锁格式化，调度类回调向 sysfs 缓冲区写 | 门内采集快照，门外格式化 |

attach 路径会提升所需能力；此后 oob 放行依据变为可信侧已记录的 attach 状态。Linux 凭据变化不再让已 attach 线程失去 oob 调用资格。这不是 attach 准入策略，谁有资格成为 EVL 线程仍是另一个问题。

睡眠重启仍检查 `restart_block.fn` 是否被其他 inband 系统调用占用，但这个输入只影响本次调用是否返回 `-EINTR`，不再提供可信定时器的日期。

sysfs 中新增 `evl_thread_gate_sched()`、`evl_thread_gate_stats()`、`evl_thread_gate_wchan()`。它们在锁内把优先级、调度类、统计或名字收集到局部副本，放锁后输出；格式化和 CPU 使用率计算移到调用侧。state、timeout、pid、observable 等简单属性继续使用发布值或返回值接口。统计的 lastperiod 仍由门更新，它只服务查询，不参与调度决策。

这里的门仍是同一地址空间内的 C 函数，参数包含线程和输出缓冲区指针，尚没有可信句柄解析及硬件隔离。局部快照也不意味着已有独立可信栈。本提交减少锁内跨域工作，并未完成全部入口防护。

Linux 的信号、ptrace、迁移、退出仍通过 Dovetail 影响 EVL 线程；嵌入的 completion、element 的 Linux 成员、用户地址访问和 oob printk 均未在这里整体解决。GET_STATE 将 state 填成 CPU 号、attach 失败泄漏名字两个已记录问题也未修复。

验证依据：既有会话记录中间提交的目标编译通过；[线程设计记录](../28-thread.md)记载最终相关改动回归 74/78，以及四万余次 sysfs 并发读取无内核告警。本次仅核对补丁和文档，没有重新执行这些测试。
