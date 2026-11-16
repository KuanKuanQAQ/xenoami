# commit 02：xbuf / proxy 按通信端拆分

内核提交 [`789a2167180f`](https://github.com/KuanKuanQAQ/linux-evl/commit/789a2167180fd8ae624819173c25337b09ef9778)，父提交 `2495805ce5b4`。只修改 `kernel/evl/xbuf.c` 和 `proxy.c`；用户库配套改动见 [commit 05](05-libevl-proxy.md)。

原实现让 inband 和 oob 共用环状态。普通线程或 Linux 文件转发者可以修改实时路径依赖的计数，并参与可信锁和唤醒协议。这里的目标是：对端给出错误数据、谎报进度或停止服务时，可信端仍然使用自己维护的地址和容量约束。通信内容和对端活性不在保证范围内。

每个环拆成三部分：

| 部分 | 内容 | 访问规则 |
|---|---|---|
| 可信端 `xbuf_tend` / `proxy_tend` | 私有 fill、off、rsvd、pending、等待队列、通知记录；proxy 另有 req、error、eof | 可信代码在本侧等待队列锁下更新 |
| inband 端 `xbuf_iend` / `proxy_iend` | 本侧下标、锁、Linux 等待对象；proxy 的 work 和 workqueue | 独立分配，由 inband 路径使用 |
| 共享结构和缓冲区 | 发布的 fill、req、等待提示及数据 | 发布值不能被可信端读回作为权威状态；输入按用途约束 |

xbuf 的 ibnd 环是 oob 写、inband 读，obnd 环反向。可信端先预留空间、放锁拷贝、重新取锁提交；同侧最后一个完成者统一发布预留量。inband 的 `inflight` 记录已完成但正在经门报告的数量，避免这段窗口内重复使用空间。

`xbuf_gate_commit()` 接收已传输数量：可信端生产时，消费报告不超过私有 fill；可信端消费时，生产报告不超过剩余容量。proxy 分别使用 `proxy_gate_consumed()`、`proxy_gate_produced()` 和 `proxy_gate_status()`；状态报告只能成为 EOF 或合法负 errno，否则转成 `-EIO`。可信端计算地址只使用本侧 off、固定 bufsz 和经过约束的长度。

出向通知复用 [commit 01](01-notify-relay.md)。xbuf 记录唤醒请求；proxy 记录启动转发者的请求，inband 的 `relay_kick()` 再排 work，kworker 执行 `kernel_read()` / `kernel_write()`，然后经门报告结果。输出转发者跳过整批数据时，下标也按整批推进；输入转发者按实际读取量推进。请求量限制在可信端认定的剩余容量内。

行为变化：

- proxy 的普通 `read()` / `write()` 返回 `-EOPNOTSUPP`；EVL 线程使用 oob I/O，普通任务可直接访问目标文件。
- proxy 的 oob poll 不再调用目标文件的 oob 回调；inband poll 保留目标普通 poll 转发。
- xbuf 的 inband 写入长度超过容量时返回 `-EINVAL`；内核接口 `evl_read_xbuf()` / `evl_write_xbuf()` 要求 oob 上下文。
- 对端停止服务时，主动请求阻塞通信的线程仍可能等待；没有新增超时策略。

proxy 的待处理通知和 relay work 仍持有 element 引用，重复排队失败时归还多取的引用，worker 完成后释放自己的引用。`destroy_ring()` 的 drain / cancel 是销毁阶段的保障，不能回收一个因为 worker 永不完成而一直被引用的对象。

本提交是逻辑拆分。门仍接受内核对象指针；fd → element、`filp->f_flags`、分配器和部分生命周期依赖未消除，不能把环内计数约束扩大解释为完整隔离。早期共享 fillsz 和 proxy_bridge 方案已被替代，详见 [最终设计](../22-pipe-final-design.md)。

验证依据：2026-11-13 会话记录该中间提交的 EVL 核心和驱动目标编译通过；最终组合的既有 QEMU 回归为 74/78，含管道压力测试。压力程序覆盖双向 xbuf 和 proxy、并发端及记录完整性；它验证正常并发语义，不是恶意内核下的隔离证明。本次收尾核对提交内容，未重跑运行测试。
