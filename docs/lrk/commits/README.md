# 六个提交的说明与配套关系

这组提交把通知中继、管道拆分、线程依赖截断和控制权限改造固定下来，并同步 libevl 的 proxy 与 TP 测试行为。按下表阅读，每个提交都有独立说明。

| 顺序 | 仓库 / 分支 | 提交 | 说明 |
|---|---|---|---|
| 01 | linux-evl / evl-lkl | `2495805ce5b4` | [通知中继](01-notify-relay.md) |
| 02 | linux-evl / evl-lkl | `789a2167180f` | [xbuf / proxy 拆分](02-xbuf-proxy.md) |
| 03 | linux-evl / evl-lkl | `7b70298bd41d` | [线程路径截断](03-thread-cut.md) |
| 04 | linux-evl / evl-lkl | `458f163d4386` | [控制权限](04-control-policy.md) |
| 05 | libevl / master | `2f2bc11e694f` | [proxy 用户接口](05-libevl-proxy.md) |
| 06 | libevl / master | `07eeff23e0b8` | [TP 测试适配](06-libevl-tp-test.md) |

内核四个提交依次叠在 `daed79c1cd9b`（LKL helper 迁移）之后；libevl 两个提交依次叠在 `db33f300adcf` 之后。内核 02 与用户库 05、内核 04 与用户库 06 配套使用。父仓库锁定本组最终版本，不改写已有提交。

既有证据：四个内核中间提交的 EVL / 驱动目标编译通过；最终组合在默认 QEMU 配置下通过 74/78（77 项 libevl 加 pipe-stress），失败为缺 gdb 的 ptrace-sync 和未启用调度类的三项 sched 测试。开启 TP/QUOTA 后 TP 两项各 3/3，quota 精度失败缺少同配置基线。详细依据见各篇及 [控制策略测试记录](../30-control-policy.md)。本次只整理文档和发布既有提交，没有重跑这些测试。

构建仍需按 [构建流程](../06-build-pipeline.md)自行生成 LKL blob；build、tools、patches 不在父仓库内。本组发布不等于已完成构建自动化或安全隔离。
