# commit 04：仅 EVL 线程可以控制 EVL 线程

内核提交 [`458f163d4386`](https://github.com/KuanKuanQAQ/linux-evl/commit/458f163d438624027b464c183f76039885bb9e60)，父提交 `7b70298bd41d`。对应用户库测试调整见 [commit 06](06-libevl-tp-test.md)。

原接口基本以“能拿到 fd”作为线程控制资格。项目明确的安全假设则是：全部 EVL 线程由维护者保证可信，可以互相管理，包括跨进程；未 attach 的普通任务不能通过 EVL 控制接口改变实时任务的运行。

新增 `evl_may_control()`，实现为 `evl_current() != NULL`。检查的是调用线程身份，不是当前执行阶段：已经 attach、暂时运行在 inband 的管理线程仍可控制其他线程。oob ioctl 入口已要求 attach，因此对应控制分支无需重复检查。

| 入口 | 受限操作 | 拒绝结果 |
|---|---|---|
| 线程 inband ioctl | SET_SCHEDPARAM、SET_MODE、CLEAR_MODE、UNBLOCK、DEMOTE、SUSPEND、RESUME | 非 EVL 调用者 `-EPERM` |
| TP / quota 调度控制 | 除 get 外的修改请求 | 同上 |
| quota_period sysfs | 修改周期 | 同上 |
| 时钟 gravity sysfs、latmus | 修改提前量、RESET / TUNE | 同上 |
| 时钟 ioctl | SET_TIME | 同上 |
| control/state stop | 尚有 EVL 用户线程时停止核心 | `-EBUSY` |

停止核心的检查与身份检查不同：即使请求者可信，有用户实时线程时也不执行批量取消。代码先进入 TEARDOWN，再计数；已越过创建检查的并发 clone 仍存在窗口，因此不能把该实现描述成严格原子的停止/创建协议。

查询不受影响，主动开放的 monitor、observable、xbuf、proxy 数据通道不属于这次控制限制。管理程序应先 attach；配套 TP 测试正是据此调整。`latmus -r` 的 reset 仍发生在 attach 前，其顺序修复不在这六个提交内。

内建 monotonic / realtime 时钟没有可设置时间的实现，可信调用者执行 SET_TIME 仍可能得到 `-EOPNOTSUPP`。限制这个 ioctl 不会阻止 Linux 自己调整 CLOCK_REALTIME 后间接改变 EVL 绝对超时。

限制边界：attach 准入仍依赖现有入口；Linux 自身的信号、ptrace、亲和性、页表和任务退出不经过这些检查。当前线程身份依赖 Dovetail 状态的信任假设。此提交是 EVL API 授权收紧，不是对恶意 Linux 的完整运行隔离。

验证依据：[控制策略文档](../30-control-policy.md)记录普通线程与已 attach 线程的权限矩阵符合预期，默认配置回归 74/78。另开 TP/QUOTA 后，TP 两项各 3/3；quota 精度测试失败且缺同配置原版对照，不能据此断言无回归。本次未重跑，只核对提交及历史记录。
