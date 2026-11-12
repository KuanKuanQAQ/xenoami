# 19 对端不可信的通道：只保护可信域，不保护通道（xbuf、proxy）

> 2026-11-10。设计稿，**没有改代码**。行号指当前工作树的 `kernel/evl/proxy.c`（786 行）和 `kernel/evl/xbuf.c`（770 行）。
> 本文给出一组通用规则，并用 proxy 逐项对照。xbuf 按同一组规则写的具体方案见 [18](18-xbuf-split.md)（第二版）。
> 相关：proxy 的 Linux 依赖盘点见 [11](11-proxy-linux-deps.md)；已撤回的 proxy 拆分稿见 [impl-shared-state/04](impl-shared-state/04-proxy-bridge-design.md)、[05](impl-shared-state/05-proxy-bridge-impl.md)，补丁在 `patches/2026-11-09-shared-state-wip/`。

---

## 0. 结论

xbuf 和 proxy 都是"一端在可信域、另一端在不可信域"的通道。不可信方写进通道的内容本来就不可信，所以**通道本身的正确性不值得保护**：下标、计数、标志被改坏，最坏也就是读到垃圾、写的东西丢了、通道卡住。这和"内容不可信"是同一个后果。

要保护的只有三件事：

1. **可信内存**：被改坏的值不能让可信代码读写共享缓冲区以外的内存；
2. **其他任务的实时性**：不可信方不能持有 oob 会去争用的锁，不能让可信代码无界地循环，也不能制造无界的唤醒；
3. **可信的控制流**：可信代码不调用不可信函数，也不顺着不可信的指针走。

按这三件事写成 7 条规则（§2）。proxy 对照的结果（§3）：**规则成立**。proxy 比 xbuf 多出三种情形，都由这 7 条规则覆盖：同一端有两个域的调用者、不可信的值被当作返回值、经目标文件的函数指针调用。

第一版 18 号文档的 reserve/commit 门、不变式 I1–I5 都是为了保护通道正确性，按本文的思路**不再需要**。门从 10 个减到 3 个（xbuf）。

---

## 1. 出发点

| 问题 | 回答 |
|---|---|
| 不可信方能改 xbuf/proxy 的下标，要不要防？ | 不用防"改"，要防"改了以后可信代码拿它算地址"。下标由可信端自己保存一份，地址只用自己那份算（规则 C2、C3） |
| 不可信方能让通道卡住，要不要防？ | 不用。选择和不可信方通信的 oob 线程只能接受这一点，就像对端进程不读管道一样。要防的是"卡住"波及其他任务（规则 C4、C5） |
| 不可信方能让 oob 线程读到垃圾，要不要防？ | 不用。内容本来就不可信 |
| 那 oob 线程得到的结果还有什么保证？ | 返回值在合法范围内（字节数不超过请求的长度，错误码是负的 errno），等待可以被信号、超时、`EVL_T_RMID` 打断。除此之外没有保证 |

这实际上是截断规则"结果可校验"的具体形式：对端写的值不纳入可信域，可信端把它当作**输入**来约束，而不是当作**状态**来保护。

---

## 2. 规则 C1–C7

（C 表示 channel，和 doc 12、13 的修正标记 C12-x、C13-x 无关。）

| # | 规则 | 防的是 |
|---|---|---|
| C1 | **共享区**：对端也要读写的东西（数据缓冲区、两端共同修改的计数、提示）放在共享区。共享区由专门的分配器分配，释放后只回到共享区，**不能复用为可信内存** | 不可信方持有旧指针，在释放以后继续写 |
| C2 | **可信端私有**：可信端自己的下标、预留量、`pending`，以及缓冲区指针、长度、粒度，都在可信内存里保存一份。写到共享区的值只是发布出去，**不读回来当真** | 不可信方改可信端自己的下标 |
| C3 | **对端的值只当输入**：可信代码从共享区读到的值：(a) 先约束到合法范围；(b) 不参与地址计算，地址只由 C2 的私有状态和已经约束过的长度决定；(c) 不当指针解引用，不当函数指针调用；(d) 作为返回值交给可信调用者之前，约束到这个返回值的合法取值 | 越界读写；错误的返回值让调用者越界 |
| C4 | **不跨域共享锁**：跨域不共用任何锁。如果同一端有两个域的调用者，不可信的调用者经门进入，由可信代码在**一次门调用**里完成整次操作（预留、拷贝、提交），门内不等待 | 不可信方持锁不放，oob 关着中断空转 |
| C5 | **有界**：可信代码不因共享区的值而无界地循环或自旋。等待只能睡在可信的等待对象上，并且可以被信号、超时或 `EVL_T_RMID` 打断 | 活锁、关中断空转 |
| C6 | **唤醒**：不可信 → 可信只经"门铃"门。参数只有句柄、方向和提示位，门铃只做唤醒，不读写共享状态，被叫醒的一方自己按 C3 重新判断。可信 → 不可信用通知中继（[18 §7.4](18-xbuf-split.md)），可信侧只登记，不调用不可信函数 | 可信代码调用不可信函数；不可信方直接操作 EVL 的等待对象 |
| C7 | **生命周期**：引用计数和对象的释放由可信侧掌握。可信侧持有的不可信对象只是一个不透明的 cookie，原样交还，自己不解引用 | 释放后使用；顺着不可信的指针走 |

**明确放弃的保证**：数据内容、计数是否正确、通道的活性、短读和唤醒的精确语义。正常情况下，也就是不可信方按协议办事时，行为和现在完全一样。

---

## 3. proxy 对照

### 3.1 结构

proxy 把一个 Linux 文件（`proxy->filp`，`build` 时 `fget(attrs.fd)` 得到，`:706`）包装起来，让 oob 线程能读写它。两个环：

| 环 | 写端 | 读端 | 转发 |
|---|---|---|---|
| output（`proxy_out`，`:46-48`） | `proxy_oob_write`（oob，`:398-416`）**和** `proxy_write`（inband VFS，`:493-512`），都调用 `do_proxy_write`（`:159-230`） | `relay_output`（kworker 或调用者上下文，`:75-143`） | `kernel_write(proxy->filp, ...)`（`:106`） |
| input（`proxy_in`，`:50-55`） | `relay_input`（`:232-298`） | `proxy_oob_read`（oob，`:418-452`）**和** `proxy_read`（inband VFS，`:514-544`），都调用 `do_proxy_read`（`:308-396`） | `kernel_read(proxy->filp, ...)`（`:265`） |

和 xbuf 的根本区别：

- **不可信的那一端是转发者**：`relay_output`、`relay_input`，不是 VFS 调用者。转发者调用 `kernel_write` / `kernel_read`，由任意 Linux 文件的驱动执行，所以它只能是不可信的。目标文件的驱动还会**直接写 `bufmem`**（`kernel_read` 写到 `ring->bufmem + wroff`，`:265`），因此缓冲区必须放在共享区（C1）。
- **可信的那一端有两个域的调用者**：output 的写端既有 oob 调用者也有 inband VFS 调用者，input 的读端也一样。xbuf 每一端只属于一个域，所以没有这个问题。

### 3.2 字段

| 字段 | 谁写 | 谁读 | 新位置 | 规则 |
|---|---|---|---|---|
| `bufmem` 的内容 | 可信写端（`:200`）；目标文件驱动经 `kernel_read`（`:265`） | `kernel_write`（`:106`）；可信读端（`:372`） | 共享区 | C1 |
| `bufmem`、`bufsz`、`granularity` 本身 | `init_ring`（`:646-648`） | 两端 | 可信端保存一份，不可信端保存一份快照 | C2 |
| `fillsz`（`atomic_t`） | output：可信端加（`:214`）、转发者减（`:116`）；input：转发者加（`:279`）、可信端减（`:385`） | 两端 | 共享区，保持原样作为共同修改的原子计数；可信端读出来以后约束到 `[0, bufsz]` | C1、C3(a) |
| output 的 `wroff`、`reserved`、`nesting` | 可信写端（`:187-191`、`:213-216`），**和** inband 的 `proxy_write`，同一把锁 | 可信写端；`can_write_buffer` 读 `reserved`（`:156`，inband 等待条件 `:507-508`） | 可信端私有。inband 写者经门（§3.4） | C2、C4 |
| output 的 `rdoff` | 转发者（`:122`） | 转发者 | 不可信端私有 | — |
| input 的 `rdoff`、`reserved`、`nesting` | 可信读端（`:359-362`、`:384-387`），**和** inband 的 `proxy_read` | 同上 | 可信端私有，inband 读者经门 | C2、C4 |
| input 的 `wroff` | 转发者（`:289`） | 转发者 | 不可信端私有 | — |
| `reqsz`（`atomic_t`） | 可信读者加（`:438`）、`oob_poll` 加（`:478`）；转发者减（`:267`、`:283`） | 转发者用作要读的字节数（`:244`）；`oob_poll` 判断是否为 0（`:476`） | 共享区，共同修改。可信侧只把它当提示（决定是否请求预读），不参与地址计算 | C1、C3 |
| `on_eof`（`atomic_t`） | 转发者置 1（`:271`），可信读者 `cmpxchg` 清 0（`:445`） | 可信读者 | 共享区。只当布尔值用 | C3(a) |
| `on_error`（`int`） | 转发者（`:269`），值来自 `kernel_read` 的返回值 | 可信读者**原样返回给 oob 调用者**（`:347-348`） | 共享区。返回之前约束（§3.5） | C3(d) |
| `lock`（hard spinlock） | 可信端和 inband VFS 调用者 | 同左 | 可信端私有，只有可信代码拿 | C4 |
| `oob_wait`（`evl_flag`） | 转发者 `evl_raise_flag`（`:138`、`:295`）；`release` | 可信等待者 | 可信。转发者经门铃（§3.6） | C6 |
| `poll_head` | 转发者 `evl_signal_poll_events`（`:131`、`:294`） | oob poller | 可信。转发者经门铃 | C6 |
| `inband_wait_r` / `inband_wait_w` | 转发者 `wake_up`（`:139`、`:297`） | inband 等待者、`proxy_poll` | 不可信 | — |
| `relay_work`（`evl_work` = `irq_work` + `work_struct`） | 可信侧 `evl_call_inband_from`（`:223`、`:441`、`:479`） | Linux 的 `irq_work_run`、workqueue | 拆开：可信侧只留通知登记项，`work_struct` 归不可信侧 | C6 |
| `wq`、`worker_lock` | 不可信侧 | 不可信侧 | 不可信 | — |
| `proxy->filp`（目标文件） | `build` | 转发者；`proxy_oob_poll` 读 `f_op`（`:487-488`） | 不可信侧持有。可信侧不再碰它（§3.7） | C3(c)、C7 |

### 3.3 函数

| 函数 | 行 | 现在 | 按规则 | 新的归属 |
|---|---|---|---|---|
| `relay_output` | 75-143 | 读写共享计数；`kernel_write`；直接调用 `evl_signal_poll_events`、`evl_raise_flag`、`evl_schedule` | 可信对象的操作改成门铃 `proxy_gate_kick(h, OUT)` | 不可信 |
| `relay_input` | 232-298 | 同上，`kernel_read` 直接写 `bufmem`；写 `on_error`、`on_eof` | 同上，`proxy_gate_kick(h, IN)` | 不可信 |
| `do_proxy_write` | 159-230 | 可信写端的预留、拷贝、提交；`running_inband()` 时**同步调用 `relay_output`**（`:218-221`） | 改为返回"需要转发"，由调用者决定怎么转发：oob 调用者登记通知，inband 调用者自己调用 `relay_output`。可信代码不再调用不可信函数 | 可信 |
| `do_proxy_read` | 308-396 | 可信读端；返回 `on_error` | `on_error` 约束后返回（§3.5） | 可信 |
| `proxy_oob_write` / `proxy_oob_read` | 398-452 | 循环 + `evl_wait_flag`；读端 `evl_call_inband_from` 请求预读 | `evl_call_inband_from` 换成通知登记 | 可信 |
| `proxy_oob_poll` | 454-491 | 读计数；请求预读；**转发到目标文件的 `oob_poll`** | 删掉转发（§3.7） | 可信 |
| `proxy_write` / `proxy_read` | 493-544 | inband VFS 入口，调用 `do_proxy_*`，也就是要拿可信锁 | 经门 `proxy_gate_write` / `proxy_gate_read`（§3.4）。等待条件改成只读共享计数 | 不可信 |
| `proxy_poll` | 546-569 | 读计数，转发到目标文件的 `poll` | 读共享计数，转发不变 | 不可信 |
| `proxy_mmap` | 571-597 | 把映射转给目标文件 | 不变 | 不可信 |
| `proxy_release` | 599-610 | `evl_flush_flag(EVL_T_RMID)` | 经门 `proxy_gate_release` | 可信（入口在 Linux） |
| `init_ring` / `destroy_ring` | 624-673 | `kzalloc` 缓冲区，`alloc_ordered_workqueue`，`evl_init_work_safe` | 缓冲区从共享区分配；workqueue 和 `work_struct` 由不可信侧创建和销毁 | 可信 + 不可信 |
| `proxy_factory_build` / `dispose` | 675-777 | `fget` 目标文件，`fput` | 目标文件由不可信侧持有和释放 | 可信 + 不可信 |

### 3.4 proxy 多出来的情形一：同一端有两个域的调用者

output 的写端上，`proxy_oob_write`（可信）和 `proxy_write`（Linux VFS 进来，不可信入口）都执行 `do_proxy_write`，并且共用 hard spinlock `ring->lock`（`:178`）、`wroff`、`reserved`、`nesting`。input 的读端也一样。

这一端不能用"每端只属于一个域"的办法拆开，因为两类写者要写进同一个环，顺序由同一把锁决定。规则 C4 的做法：

```c
/* 门：不可信的 proxy_write 调用；可信代码在一次调用里完成整次写 */
ssize_t proxy_gate_write(proxy_handle_t h, const char __user *u_buf,
			 size_t count, bool *need_relay)
{
	struct evl_proxy *proxy = proxy_of_handle(h);       /* 句柄 → 可信对象 */
	return proxy_end_write(proxy, u_buf, count, need_relay);  /* 原 do_proxy_write，去掉 :218-224 */
}
```

- 门内只有"取锁、预留、放锁拷贝、取锁、提交、放锁"，**不等待**。返回 `-EAGAIN` 时，不可信的 `proxy_write` 自己在 `inband_wait_w` 上等，等待条件只读共享的 `fillsz`（原来 `can_write_buffer` 还读了可信的 `reserved`，`:156`）。等待条件只决定"醒来重试"，真正的判断在门里重做。
- `*need_relay` 为真（空 → 非空）时，由不可信的调用者自己调用 `relay_output`。可信代码不调用它。
- 不可信方经门能做到的最坏的事：写入很多垃圾，或者在拷贝的时候让用户页缺页，拖长 `nesting > 0` 的时间，从而推迟 oob 写者数据的发布。这些都只影响这个 proxy，属于放弃的活性保证。锁只在门内取放，持有时间是常数。

读端对称：`proxy_gate_read(h, u_buf, count)`，内容就是原来的 `do_proxy_read`。

**另一种做法**：inband 写者不进环，直接 `kernel_write` 目标文件。这样能省掉门，但 inband 写和 oob 写之间的顺序就丢了。原来的语义里两者是有序的，所以不建议。

### 3.5 proxy 多出来的情形二：不可信的值被当作返回值

`do_proxy_read` 在数据不够时 `return in->on_error`（`:347-348`）。`on_error` 来自转发者，也就是 `kernel_read` 的返回值（`:269`），对可信侧来说是任意整数。如果它是一个正数，比如 5000，oob 读者就会认为读到了 5000 字节。实际上用户缓冲区没有被写，而且 5000 还可能超过请求的长度 `count`。这违反了 §1 里给 oob 调用者的唯一保证。

按 C3(d) 约束：

```c
int e = READ_ONCE(sh->on_error);
if (e)
	return (e < 0 && e >= -MAX_ERRNO) ? e : -EIO;
```

同类的检查：所有"从共享区读出、最后成为可信函数返回值"的地方都要做。xbuf 没有这种情况，xbuf 的返回值都由可信端自己算出来。

### 3.6 唤醒：门铃和通知中继

| 方向 | 现在 | 新的 |
|---|---|---|
| 可信 → 不可信：output 空 → 非空，要启动转发 | `evl_call_inband_from(&ring->relay_work, ring->wq)`（`:223`），即 oob 上的 `irq_work_queue`，再由 irq_work 回调 `queue_work` | 可信侧登记通知（[18 §7.4](18-xbuf-split.md) 的中继）；不可信的处理函数自己 `queue_work(wq, ...)` |
| 可信 → 不可信：input 请求预读 | `atomic_add(reqsz)` + `evl_call_inband_from`（`:438-441`、`:478-479`） | `reqsz` 在共享区照常加；通知同上 |
| 不可信 → 可信：output 有空间了 | `relay_output` 直接调用 `evl_signal_poll_events(POLLOUT)`、`evl_raise_flag`、`evl_schedule`（`:131-142`） | `proxy_gate_kick(h, OUT)` |
| 不可信 → 可信：input 有数据，或者出错 / EOF | `relay_input` 直接调用 `evl_signal_poll_events(POLLIN)`、`evl_raise_flag`（`:293-297`） | `proxy_gate_kick(h, IN)` |
| 不可信 → 不可信 | `wake_up(inband_wait_w / _r)`（`:139`、`:297`） | 不变 |

门铃 `proxy_gate_kick(h, dir, pollhint)` 的内容：按 `pollhint` 决定是否 `evl_signal_poll_events`，然后 `evl_raise_flag`，最后 `evl_schedule`。`pollhint` 由转发者给出（output 方向原来是 `count == 0`，`:130`），谎报只会让 oob poller 多醒一次或少醒一次。被叫醒的 oob 读写者自己重新读共享计数，并按 C3 约束。

**门铃不做去重**。起初想过"只在计数变化时才唤醒"，但有两个问题。第一，它挡不住风暴，因为不可信方每次都可以真的改计数。第二，它会丢唤醒：两次敲门铃之间，如果对端的消费和可信端的提交正好相互抵消，计数没有变化，但可信端的预留量变了，等待者其实应该醒来（推导见 18 §7.2）。风暴另外处理，见 U1。

和 xbuf 相比，通知中继在 proxy 里的处理动作是 `queue_work`，在 xbuf 里是 `wake_up`。动作不同，登记和取走的机制相同。所以中继应该做成公共设施，可信侧只交还 cookie，动作由不可信侧决定（§5）。

### 3.7 proxy 多出来的情形三：经目标文件的函数指针调用

`proxy_oob_poll` 的最后一步（`:487-488`）：

```c
if (proxy->filp->f_op->oob_poll)
	ret |= proxy->filp->f_op->oob_poll(filp, wait);
```

这一步在 oob 上顺着 Linux 对象 `filp` 读 `f_op`，再调用一个函数指针。这违反了 C3(c)，也违反了"可信代码不调用不可信代码"。另外，它传的是 proxy **自己的** `filp`，不是目标文件的。11-04 读代码时确认过：目标是 socket 时，`sock_oob_poll` 会把 proxy 的 `evl_file_binding` 当成 `evl_socket` 来用（见 log 11-04、已撤回的 impl-shared-state/01，未运行复现）。

建议**删掉这一步**，和已撤回的补丁 `impl-shared-state/01-drop-oob-poll-forward` 做法相同。oob 版本的 poll 用预读代替转发，这在原来的 `:469-481` 已经实现了。

### 3.8 原来读代码发现的 proxy 问题，在新思路下的位置

impl-shared-state/04 记过几个转发者的问题（未验证）：

| # | 问题 | 新思路下 |
|---|---|---|
| P2 | `relay_output` 出错或写 0 时，`rdoff` 落后于消费量 | 转发者私有状态出错，只影响这个通道。不再是可信域的问题 |
| P4 | `relay_input` 短读时 `wroff` 前进 `n`，而 `fillsz` 只加了 `ret` | 同上。可信读者从 `fillsz` 读到的数和数据对不上，读到的是垃圾，但地址由可信端自己的 `rdoff` 决定 |
| P5 | `reqsz` 可能超过剩余空间，导致覆盖 | 同上 |
| P7（doc 14） | `proxy_release` 叫醒同一个 element 所有 fd 上的 oob 等待者 | 和本文无关，仍待确认 |

这几个问题仍然是 bug，值得修，但不再影响划分。这正是本文思路的好处：**不可信一侧的 bug 不会扩大可信域的范围**。

### 3.9 不可信方能做什么

| 能 | 不能 |
|---|---|
| 写坏、丢掉、编造通道里的数据 | 让可信代码读写共享缓冲区以外的内存（C2、C3） |
| 改 `fillsz`、`reqsz`、`on_eof`，让 oob 读写者读到垃圾、拿到短读、一直等待 | 让 oob 读者得到超过请求长度的返回值，或者非法的错误码（C3(d)） |
| 经门写入大量数据，或者在门内拷贝时缺页，推迟这个 proxy 上 oob 数据的发布 | 持有可信锁不放（C4：锁只在门内取放） |
| 频繁敲门铃，叫醒 oob 等待者（§6 的 U1） | 让门铃做唤醒以外的事（C6） |
| 不处理通知，让转发停下来 | 让可信代码调用目标文件的任何函数（§3.7） |
| | 影响别的 proxy 或别的 EVL 对象（句柄校验、C7） |

---

## 4. xbuf 和 proxy 对照

| 方面 | xbuf（[18](18-xbuf-split.md)） | proxy |
|---|---|---|
| 不可信的一端 | inband VFS 调用者：ibnd 的读端、obnd 的写端 | 转发者 `relay_*`，加上目标文件驱动直接写缓冲区 |
| 可信的一端 | ibnd 的写端（oob、`evl_write_xbuf`）、obnd 的读端 | output 的写端、input 的读端，**两端都还有 inband VFS 调用者** |
| 跨域的锁 | 环锁由两端共用（Q2） | 环锁由可信端和 inband VFS 调用者共用；转发者原本就不拿 |
| 共同修改的计数 | `fillsz`，原来在锁内修改，要改成原子计数 | `fillsz`，**本来就是原子计数**；另有 `reqsz` |
| 不可信 → 可信的唤醒 | inband 提交时调用回调（`evl_raise_flag_nosched`、`evl_flush_wait_locked`） | 转发者直接调用 `evl_raise_flag`、`evl_signal_poll_events`、`evl_schedule` |
| 可信 → 不可信的唤醒 | `irq_work_queue` → `wake_up` | `evl_call_inband_from` → `irq_work_queue` → `queue_work` |
| 不可信的值成为返回值 | 无 | `on_error`（§3.5） |
| 可信代码经不可信函数指针调用 | 无 | `f_op->oob_poll`（§3.7） |
| 需要的门 | 门铃、通知取走、release（3 个） | 门铃、通知取走、release、`gate_write`、`gate_read`（5 个） |

**proxy 的形状其实更接近目标**：转发者本来就不拿 `ring->lock`，`fillsz` 本来就是原子计数。xbuf 的第二版方案相当于让 xbuf 采用 proxy 现有的形状：可信端私有下标，共享一个原子 `fillsz`。

---

## 5. 公共设施

两个子系统都要用、以后 monitor、observable 也会用到的东西：

| 设施 | 内容 | 用户 |
|---|---|---|
| 共享区分配器 | 分配缓冲区和共享计数，释放后不回到可信内存（C1） | xbuf、proxy |
| 通知中继 `evl_inband_notify` | 可信侧的 `post(n)`：`test_and_set_bit` → 挂到可信的 per-CPU 链表 → `irq_post_inband(合成中断)`。不可信的处理函数经门 `take` 取走 cookie，自己决定做什么（`wake_up`、`queue_work`） | xbuf（叫醒 inband 读写者）、proxy（启动转发）。以后用来替代所有可信对象里的 `irq_work`（16-subsystems/README 的结论） |
| 门铃的写法 | `kick(h, dir, pollhint)`：句柄 → 可信对象；按提示发 poll 事件；叫醒这个方向的 oob 等待者；`evl_schedule`。不读共享状态，不去重 | xbuf、proxy |
| 共享计数的读法 | `fill_of(atomic_t *, bufsz)`：小于 0 当 0，大于 `bufsz` 当 `bufsz` | xbuf、proxy |

---

## 6. 待确认的问题

| # | 问题 | 说明 | 建议 |
|---|---|---|---|
| U1 | 跨 CPU 的唤醒风暴 | 不可信方在 CPU1 上可以不停地敲门铃，每次都会经 IPI 叫醒 CPU0 上的 oob 线程。这个线程自己吃亏是它的选择，但它的优先级高，会挤占 CPU0 上低优先级的 oob 任务 | 先记录下来。可选的办法：门铃按对象限频；或者只在"等待者的条件可能满足"时才唤醒，比如记下等待者要的长度 |
| U2 | 门内拷贝用户内存时缺页 | 可信代码在门里（inband）`copy_from_user`，缺页时会执行 Linux 的缺页处理。oob 路径上原本就有同样的问题（doc 17：`monitor_oob_write` 用非 raw 的 `copy_from_user`） | 归入"用户内存是第三个域"的问题（doc 11 #10），不在本文范围 |
| U3 | 可信端的哪些调用者属于可信域 | `proxy_oob_write` 对非 EVL 线程会在 inband 上执行（log 11-05）；`evl_write_xbuf` 也可能在 inband 上以 `O_NONBLOCK` 调用。它们仍然是可信代码 | 不影响规则。可信与否看代码，不看 stage（doc 14） |
| U4 | 句柄的形式 | 门要根据句柄找到可信对象 | 随 `evl_element` 拆分一起决定（18 §10） |
| U5 | 正常情况下是否完全等价 | `fill_of` 的约束在不可信方守规矩时不起作用；门铃的唤醒和原来的回调一一对应，只是改在对端放锁以后执行 | 实施时用 libevl 的 `basic-xbuf`、`poll-xbuf` 和 proxy 测试（`proxy-echo`、`proxy-eventfd`、`proxy-pipe`、`proxy-poll`）确认 |

---

## 7. 实施顺序

两个子系统一起做时：

| 步骤 | 内容 | 改变行为？ |
|---|---|---|
| 1 | 公共设施：`fill_of`、通知中继（先在 EVL 里实现，处理函数仍由 EVL 提供，相当于 impl-shared-state/03） | 否 |
| 2 | xbuf 按 18 第二版拆分：可信端私有，原子 `fillsz`，门铃，通知中继 | 否 |
| 3 | proxy：`do_proxy_write` 不再调用 `relay_output`；`on_error` 约束；删除 `oob_poll` 转发 | 第 3 项改变行为（原来是 bug） |
| 4 | proxy：inband VFS 入口经 `gate_write` / `gate_read`；转发者经门铃；`proxy_inband` 结构从可信对象里分出去 | 否 |
| 5 | 两者的不可信部分挪到独立的 `.c` 文件，由编译器保证边界（`nm` 检查只引用门） | 否 |
