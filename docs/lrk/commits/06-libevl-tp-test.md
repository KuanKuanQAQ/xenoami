# commit 06：TP 精度测试先 attach 再安装调度表

用户库提交 [`07eeff23e0b8`](https://github.com/KuanKuanQAQ/libevl/commit/07eeff23e0b818d3d4cc5f544ff6ed27276a70e6)，父提交 `2f2bc11e694f`。只修改 `tests/sched-tp-accuracy.c`，适配 [内核 commit 04](04-control-policy.md)。原消息引用内核策略说明，本文单独记录测试变化。

测试 main 原来设置 CPU 亲和性后，直接调用 `evl_control_sched()` 安装 TP 调度表。新权限策略下，未 attach 的 main 属于普通线程，安装会返回 `-EPERM`，测试还未进入精度测量就失败。

提交在设置亲和性后、安装调度表前增加 `evl_attach_self()`，并沿用测试框架断言检查结果。它让安装者成为可信 EVL 线程；测量算法、TP 窗口和精度阈值均未修改。

测试需要启用 `CONFIG_EVL_SCHED_TP`。默认 QEMU 配置没有打开该调度类，所以默认回归中的此项失败不能用于判断适配是否有效。

[既有记录](../30-control-policy.md)显示，在开启 TP/QUOTA 的配置下，此项与 sched-tp-overrun 各运行三次通过。sched-quota-accuracy 约 108% 的精度失败属于另一项测试，缺少同配置基线；本提交没有修复或掩盖它。本次收尾没有重跑运行测试。
