# 01 `proxy_oob_poll` 不再转发到目标文件的 `oob_poll`（会改变行为）

> **修订说明（2026-11-04）**：这一项最初的做法是"把目标文件的 `f_op->oob_poll` 存成快照"（属于 §11 第一类，不改变行为）。讨论后发现这一步转发本身在语义上不成立，而且在一种实际场景下会调用一个乱指的函数指针，所以改成**直接删除这一步转发**，快照也随之去掉。被替代的方案见 §7。

## 1. 原本的机制

### 1.1 poll 是什么

poll 用来"同时等一组 fd，哪个准备好了就告诉我"，和 Linux 的 `poll` / `epoll` 一样。应用不能直接用阻塞的 `oob_read` 来做这件事：那样线程只能睡在一个 fd 上；而如果改成非阻塞地挨个去读，就成了忙轮询。poll 让线程只睡一次，任何一个来源就绪都能把它叫醒。

EVL 在 oob 上做 poll 时，会调用每个 fd 的 `oob_poll`，问它两件事：

1. **现在准备好了吗？** 返回一组标志位：`POLLOUT` 表示可写，`POLLIN` 表示可读。
2. **还没准备好的话，好了请通知我。** 也就是把等待者登记到这个文件上。

注意，poll 的结果只是一个提示，不保证之后的读写一定不阻塞。应用的正确用法是：把 fd 设成非阻塞，poll 说就绪就去读写，遇到 `-EAGAIN` 就回去重新 poll。proxy 上这一点尤其明显：poll 报告 `POLLOUT` 的条件是 ring 里"至少还有 1 字节空位"（`proxy.c:477`），而写入要求"整块数据都放得下"（`can_write_buffer`，`proxy.c:164`）。

### 1.2 `proxy_oob_poll` 原来做什么

`proxy_oob_poll`（改动前 `proxy.c:454`）汇总三类事件：

| 步骤 | 检查什么 | 结论 |
|---|---|---|
| 1 | `evl_poll_watch(&proxy->poll_head, wait, NULL)` | 把等待者登记到 proxy 上；之后 `relay_output` / `relay_input` 里的 `evl_signal_poll_events` 会叫醒它 |
| 2 | 输出 ring 还有空位吗 | 有就返回 `POLLOUT` |
| 3 | 输入 ring 有数据吗 | 有就返回 `POLLIN`；没有就把 `reqsz` 加一点，请 inband 去目标文件预读 |
| 4 | **目标文件有没有 oob 事件** | 如果目标的 `f_op->oob_poll` 不为空，调用它，把结果合并进来 |

第 4 步的原代码（改动前 `proxy.c:483-488`）：

```c
	/*
	 * If the proxied file implements an oob poll handler, collect
	 * the events pending there as well.
	 */
	if (proxy->filp->f_op->oob_poll)
		ret |= proxy->filp->f_op->oob_poll(filp, wait);
```

- `proxy->filp`：被代理的目标文件；
- `->f_op->oob_poll`：目标文件操作函数表里的 oob poll 函数，只有支持 oob 的文件才有；
- 整句的意思：如果目标文件也支持 oob poll，就也问问它，用按位或把它的标志合并进 proxy 的结果。

inband 版的 `proxy_poll`（`proxy.c:558`）表面上是对称的：问完自己的 ring，再问目标的普通 `poll`（`proxy.c:577-578`）。但两者的读写路径并不对称，见 §2.4。oob 版之所以问 `oob_poll` 而不是普通 `poll`，是因为普通 `poll` 要用 Linux 的等待队列和锁，不能在 oob 上调用。

### 1.3 第 4 步读了哪些 Linux 共享状态

| 访问 | 对象 | 归属 |
|---|---|---|
| `proxy->filp->f_op` | 目标文件的 `struct file`，偏移 0x10 | 与 Linux 共享（目标挂在应用的 fd 表里） |
| `f_op->oob_poll` | 目标驱动的 `struct file_operations` | 目标驱动的静态只读数据 |
| 调用 `oob_poll(...)` | 目标驱动的代码 | 取决于目标 |

## 2. 为什么这一步不成立

### 2.1 proxy 的读写从来不走目标文件的 oob 路径

以读为例，`proxy_oob_read`（`proxy.c:429`）的流程：

| 步骤 | 函数 | 位置 | 做什么 |
|---|---|---|---|
| 1 | `do_proxy_read` | `proxy.c:319` | 只从输入 ring 取数据；不够就返回 0 |
| 2 | `proxy_oob_read` | `proxy.c:444-453` | 把要读的字节数加到 `reqsz`，`evl_call_inband_from` 请 inband 帮忙，睡在 `oob_wait` 上 |
| 3 | `relay_input` | `proxy.c:243` 起（inband，kworker） | `kernel_read(filp, ...)` 从目标文件读进 ring，`evl_raise_flag(&ring->oob_wait)` 叫醒读者 |
| 4 | 回到步骤 1 | | 从 ring 里取 |

写方向同理：`do_proxy_write` 只写 ring，由 inband 的 `relay_output` 调 `kernel_write`。目标文件的 `oob_read` / `oob_write` 从来不会被调用。所以，"目标文件现在可以 oob 读写"和"proxy 现在读写会不会阻塞"没有关系。把目标的 oob 事件合并进 proxy 的结果，只会误导应用：例如目标报告 `POLLIN`，proxy 的输入 ring 却是空的，应用一读就阻塞。

### 2.2 目标是 EVL 设备时，proxy 本来就转发不了

`kernel_read` 要求目标实现 `read_iter`，而且不能同时实现 `read`（`fs/read_write.c:521`）：

```c
	if (unlikely(!file->f_op->read_iter || file->f_op->read))
		return warn_unsupported(file, "read");
```

`kernel_write` 同理（`fs/read_write.c:611`，要求 `write_iter`）。EVL 设备用的是 `read` / `write`，例如 xbuf 的 `.read = xbuf_read`、`.write = xbuf_write`（`kernel/evl/xbuf.c:563-564`）。所以拿 proxy 包一个 EVL 设备，`relay_input` / `relay_output` 会得到 `-EINVAL`。"目标是带 `oob_poll` 的 EVL 设备"这种情况，proxy 本身就不支持；而且 EVL 设备本来就能直接 `oob_read` / `oob_write`，没有必要包一层 proxy。

### 2.3 真正会走到第 4 步的场景：目标是 socket，而且会出错

只有同时实现了 `read_iter` / `write_iter` 和 `oob_poll` 的文件，才会走到第 4 步。打开 EVL 网络栈（`CONFIG_EVL_NET`，依赖 `CONFIG_NET_OOB`）之后，所有 socket 都满足：

- `socket_file_ops` 有 `.read_iter = sock_read_iter`、`.write_iter = sock_write_iter`（`net/socket.c:260-261`），也有 `.oob_poll = sock_oob_poll`（`net/socket.c:268`）；
- `socket_file_ops` 是所有 socket 共用的，所以连普通的 inband TCP socket 也带着 `oob_poll`。

"用 proxy 把 oob 线程的日志转发到一个 TCP socket"是很自然的用法。这时 EVL 的 `sock_oob_poll`（`kernel/evl/net/socket.c:845`）会被调用：

```c
__poll_t sock_oob_poll(struct file *filp, struct oob_poll_wait *wait)
{
	struct evl_socket *esk = evl_sk_from_file(filp);

	if (esk == NULL)
		return -EBADFD;

	return esk->proto->oob_poll(esk, wait);
}

static inline struct evl_socket *evl_sk_from_file(struct file *filp)   /* socket.c:64 */
{
	return filp->f_oob_state.data ?
		container_of(filp->f_oob_state.data, struct evl_socket, efile) :
		NULL;
}
```

**按原代码传的参数（`filp`，proxy 自己的文件）**：

1. proxy 文件的 `f_oob_state.data` 不为空，指向 proxy 自己的 `evl_file`（`evl_open_file` 设的）；
2. `container_of` 把 proxy 的 `evl_file_binding` 当成 `evl_socket`；
3. `esk->proto` 读到的是 proxy 内存里某个位置的值，被当作函数表去调 `oob_poll`；
4. 结果是**调用一个乱指的函数指针**，基本就是内核崩溃。

**即使把参数改成 `proxy->filp`（目标 socket）**，普通 inband socket 的 `f_oob_state.data` 为空，`sock_oob_poll` 返回 `-EBADFD`。这个负数被当成 `__poll_t` 位掩码，几乎所有位都是 1，合并后 `POLLIN`、`POLLOUT`、`POLLERR` 全被置上，poll 永远报告"就绪"，应用陷入忙循环。

> 以上 §2.3 是读代码推出来的，**没有运行复现**。QEMU 配置没有打开 `CONFIG_EVL_NET`（`build/linux-evl-qemu/.config:364`），复现需要换用打开网络栈的配置，见 §5。

### 2.4 inband 版为什么不一样

inband 版 `proxy_poll` 转发目标的普通 `poll` 是**有道理的**，因为 inband 的读写会在调用者自己的上下文里同步访问目标文件：

- `proxy_read`（`proxy.c:525`）发现 ring 不够时，直接调用 `relay_input(proxy)`（`proxy.c:546`），在调用者的上下文里 `kernel_read` 目标文件，而不是交给 kworker；
- `do_proxy_write` 在 inband 上遇到 ring 由空变非空时，直接调用 `relay_output`（`proxy.c:231`），在调用者的上下文里 `kernel_write` 目标文件。

所以在 inband 上，"目标文件可读、可写"确实关系到 proxy 的读写会不会阻塞。另外，inband 版**不做预读**：输入 ring 空的时候，没有任何 proxy 侧的事件会来叫醒 poller，唯一的唤醒来源就是转发 `wait` 后挂在目标文件等待队列上的那一项。转发在这里是必需的。

oob 版则相反：oob 的读写从不同步访问目标（§2.1），也不能挂到目标的 Linux 等待队列上，所以它改用"输入 ring 空时踢一下 inband 预读"（`proxy.c:484-491`），预读完成后由 `relay_input` 里的 `evl_signal_poll_events` 叫醒 poller。**预读就是 oob 版对"转发"的替代**，再转发目标的 `oob_poll` 是多余的。

### 2.5 小结

| 目标文件的类型 | 第 4 步 | 后果 |
|---|---|---|
| 普通文件、管道、终端（最常见，例如 libevl 的 stdout proxy） | `oob_poll` 为 NULL，跳过 | 无害，也没用 |
| EVL 设备（xbuf 等） | 会执行 | proxy 本来就转发不了（`kernel_read` 返回 `-EINVAL`），而且参数传错 |
| socket（打开 `CONFIG_EVL_NET` 时） | 会执行 | 参数传错时调用乱指的函数指针；改对参数也会让 poll 永远报告就绪 |

第 4 步在任何情况下都没有正面作用。

## 3. 修改思路

- **删除 oob 版的第 4 步**：proxy 的 oob poll 只反映自己两个 ring 的状态，和 `oob_read` / `oob_write` 的实际行为一致。
- **顺带的效果**：oob poll 路径完全不再访问目标文件的 `struct file`，也不再有间接调用。这比最初的"快照"方案更彻底：快照只是把读 Linux 内存换成读 EVL 的拷贝，删除则是根本不访问（对应 doc 13 §8.3 的最后一种处理："不访问"）。
- **inband 版 `proxy_poll` 不动**：它转发目标的普通 `poll` 是有道理的（§2.4），参数传的也是正确的 `proxy->filp`。它唯一的不精确之处是合并结果时没有按方向过滤，例如只开了输出方向的 proxy，也会把目标的 `POLLIN` 合并进来。这个问题很小，而且它只在 inband 上运行，不在本次范围内。
- **这会改变行为**：如果有应用依赖"poll proxy 时顺便拿到目标文件的 oob 事件"，它会受影响。按 §2 的分析，现实中能走到这一步的场景要么不工作（EVL 设备），要么会崩溃（socket），所以不应存在依赖它的正常应用。

## 4. 具体修改（`kernel/evl/proxy.c`）

`proxy_oob_poll`（`proxy.c:465` 起），删除第 4 步，换成一段说明：

```c
	/*
	 * Readiness only depends on the rings: oob_read/oob_write
	 * never reach the proxied file directly, data is always
	 * relayed in-band by kernel_read/kernel_write. So we do not
	 * collect events from the oob poll handler of the proxied
	 * file, if any.
	 */

	return ret;
```

`struct evl_proxy` 和 `proxy_factory_build` 与原代码相同（最初方案加的 `target_oob_poll` 字段和赋值已经去掉）。

## 5. 验证

**静态检查**（已做）。对比原代码和改动后的 `proxy.o` 中 `proxy_oob_poll` 的反汇编：

| | 访问 `struct file` 字段 | 调用 |
|---|---|---|
| 改动前 | `mov 0x10(%rax),%rax`：读目标的 `f_op` | `call *%rax`：间接调用目标的 `oob_poll`；`evl_call_inband_from` |
| 改动后 | 无 | 只剩 `evl_call_inband_from` |

`proxy.o` 的外部符号前后完全相同。`build/linux-evl-qemu` 和 `build/linux-evl-full` 两种配置编译通过，无警告。

**运行测试**（未做，计划）：

1. libevl 的 `tests/proxy-poll.c`、`tests/proxy-eventfd.c` 应照常通过：它们的目标是 eventfd、管道这类没有 `oob_poll` 的文件，行为不变。
2. **socket 场景的复现与回归**（需要打开 `CONFIG_EVL_NET` 的内核）：建一个普通 TCP 或 UDP socket，用 proxy 包住它，在 oob 线程里对 proxy 做 `evl_poll`。
   - 在原代码上，预期会因调用乱指的函数指针而崩溃，这可以验证 §2.3 的推理；
   - 在新代码上，poll 应只按 ring 的状态返回。

## 6. 局限

- oob 路径上仍然有对 `struct file` 的读：`element_of(filp)` 读 proxy 自己文件的 `private_data`（偏移 0x20），属于初始化后不变。要彻底消除，需要让 oob 系统调用分发直接把 `evl_file` 传给处理函数，改动面大，不在本次范围内。
- inband 版 `proxy_poll` 合并目标事件时不按方向过滤，没有处理，见 §3。

## 7. 被替代的方案：快照（记录备查）

最初的做法是在 `struct evl_proxy` 里加一个字段 `target_oob_poll`，`proxy_factory_build` 在 `fget` 之后执行 `proxy->target_oob_poll = filp->f_op->oob_poll;`，oob poll 改为调用这份快照。理由是 `f_op` 在 open 之后不再变化，属于 doc 13 §11.3 的第一类。

它能消除对目标 `struct file` 的读，但保留了第 4 步的全部语义问题，以及 §2.3 那个参数错误。讨论之后改为删除。
