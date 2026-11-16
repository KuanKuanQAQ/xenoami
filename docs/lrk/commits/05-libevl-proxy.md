# commit 05：libevl 的 proxy 改用 oob I/O

用户库提交 [`2f2bc11e694f`](https://github.com/KuanKuanQAQ/libevl/commit/2f2bc11e694fbb8b3978b1305e8eab9976d4649b)，父提交 `db33f300adcf`。只修改 `lib/proxy.c`，配合 [内核 commit 02](02-xbuf-proxy.md)。原提交消息指向该内核说明，本文为用户库提交提供独立说明。

内核删除 proxy 的 inband 读写后，原先按执行阶段选普通或 oob 调用的 `__evl_transparent_call()` 会在 inband 路径得到 `-EOPNOTSUPP`。此外该宏适用于状态返回值，原用法会丢失成功读写的字节数。

`evl_write_proxy()` / `evl_read_proxy()` 现在直接调用 `oob_write()` / `oob_read()`，成功返回实际字节数，失败转成负 errno。已 attach 的线程即使当前在 inband，也由 oob 调用路径切换；未 attach 的线程调用普通 proxy I/O 得到 `-EPERM`。

对标准输出做兼容：`evl_vprint_proxy()` 发现调用者未 attach，且使用内部 stdout/stderr proxy 时，直接写对应原始流。这个特例不会让未 attach 线程访问任意 proxy。它也不保证与仍在 proxy 环中排队的输出之间的全局顺序。

接口签名和 fd 形式保持不变；可信句柄 ABI 尚未实现。固定格式化缓冲区等既有实现不属于本提交调整范围。

验证依据：既有完整组合的 QEMU 回归及 pipe-stress 包含 proxy 双向通信。本次核对 `git show` 中的调用和返回值修改，未补报独立运行测试。部署时应与内核 commit 02 及其后续版本配套更新。
