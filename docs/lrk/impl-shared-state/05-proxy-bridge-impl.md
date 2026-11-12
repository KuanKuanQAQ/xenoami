# 05 proxy 拆成 EVL 侧和 Linux 侧：逐处修改说明

> **状态**：已实现，2026-11-05，未提交，未运行测试（只编译过）。
> 设计依据是 [04](04-proxy-bridge-design.md)，实现时做了几处简化，见最后一节。阅读本篇不需要先读 04。
> 补丁：[patches/proxy-bridge.diff](patches/proxy-bridge.diff)（相对于 01、02 改完之后的 proxy.c）。

---

## 0. 一句话和一张图

**一句话**：proxy 里只在 inband 运行的那部分代码（转发者 `relay_output` / `relay_input`，以及 inband 的 `write` / `read` / `poll` / `mmap`），挪到一个新文件 `proxy_bridge.c` 里。它们原来直接读写 EVL 的状态，现在只能调用 `proxy.c` 提供的一组小函数，这些小函数叫"门"。

```
             proxy.c（EVL 侧，可信）                    proxy_bridge.c（Linux 侧，非可信）
  ┌────────────────────────────────────┐          ┌──────────────────────────────────────┐
  │ struct evl_proxy                   │          │ struct proxy_bridge                   │
  │   output.ring ─┐                   │          │   proxy  (不透明指针，只能传给门)       │
  │   input.ring  ─┼─ struct proxy_ring│          │   filp   (目标文件)                    │
  │   input.reqsz/on_eof/on_error      │          │   out ─┐                              │
  │   poll_head                        │          │   in  ─┴─ struct proxy_relay          │
  │   bridge ──────────────────────────┼────────► │        off / worker_lock / inband_wait│
  │                                    │          │                                      │
  │ oob 入口：proxy_oob_write/read/poll │  门      │ relay_output / relay_input           │
  │ 门：proxy_gate_*  ◄────────────────┼──────────┤ proxy_bridge_write/read/poll/mmap    │
  └────────────────────────────────────┘          └──────────────────────────────────────┘
                     │                                          │
                     └──────────────► bufmem（数据区，共享）◄───┘
```

**三个文件**：

| 文件 | 属于 | 内容 |
|---|---|---|
| `kernel/evl/proxy.c` | EVL | EVL 侧结构体；oob 入口；门；创建和销毁 |
| `kernel/evl/proxy_bridge.c`（新） | Linux | Linux 侧结构体；两个转发者；四个 inband 文件操作 |
| `kernel/evl/proxy_gate.h`（新） | 边界 | 两边互相调用的函数声明。两个结构体在这里只有名字，没有定义 |

**一条规则，由编译器检查**：`proxy_bridge.c` 只包含 `proxy_gate.h`，看不到 `struct evl_proxy` 的定义，所以它没法写 `proxy->output.ring.fillsz`，想碰 EVL 状态只能调门。我故意在 `proxy_bridge.c` 里加了一行 `br->proxy->bridge`，编译报错 `invalid use of undefined type 'struct evl_proxy'`。

**行为**：除了 §17 说的一处时序细节以外，和改之前完全一样。P1–P6 那几个疑似 bug 都原样保留。

---

## 1. 结构体的改造

### 1.1 原来：一个 `proxy_ring` 装了两拨人的东西

原来每个方向一个 `struct proxy_ring`，里面的字段由两拨人使用：

- **EVL 这一端**：输出方向的 EVL 写者，输入方向的 EVL 读者。它们可能在 oob 上运行。
- **转发者这一端**：`relay_output` / `relay_input`，以及 inband 的读写入口。它们只在 inband 上运行。

```c
struct proxy_ring {               /* 原来 */
	void *bufmem;                 /* 两端都用：数据区 */
	atomic_t fillsz;              /* 两端都用：交接计数 */
	int nesting;                  /* EVL 端 */
	unsigned int bufsz;           /* 两端都用：不变 */
	unsigned int rdoff;           /* 输出方向：转发者用；输入方向：EVL 读者用 */
	unsigned int wroff;           /* 输出方向：EVL 写者用；输入方向：转发者用 */
	unsigned int reserved;        /* EVL 端 */
	unsigned int granularity;     /* 两端都用：不变 */
	struct evl_flag oob_wait;     /* EVL 端：oob 等待者 */
	wait_queue_head_t inband_wait_r;  /* 转发者端 */
	wait_queue_head_t inband_wait_w;  /* 转发者端 */
	struct evl_work relay_work;   /* EVL 端：发起转发 */
	hard_spinlock_t lock;         /* EVL 端 */
	struct workqueue_struct *wq;  /* EVL 端：发起转发时要用 */
	struct mutex worker_lock;     /* 转发者端 */
};
```

问题在于：转发者属于 Linux 侧（它是 kworker，或者 inband 调用者），它自己的东西（`rdoff`/`wroff` 中的一个、`worker_lock`、inband 等待队列）却放在 EVL 对象里；同时它还能随手改 EVL 的东西（`fillsz`、`oob_wait`）。

### 1.2 现在：按"谁用"分到两边

**分法只有一条：只有转发者用的，搬到 Linux 侧；EVL 端用的，留在 EVL 侧；`bufmem` 两边都用，是共享数据区。**

**EVL 侧**（`proxy.c:34`）：

```c
struct proxy_ring {
	void *bufmem;
	atomic_t fillsz;
	int nesting;
	unsigned int bufsz;
	union {                       /* EVL 侧只留自己那一端的下标 */
		unsigned int wroff;       /* 输出 ring：EVL 写者 */
		unsigned int rdoff;       /* 输入 ring：EVL 读者 */
	};
	unsigned int reserved;
	unsigned int granularity;
	struct evl_flag oob_wait;
	struct evl_work relay_work;
	hard_spinlock_t lock;
	struct workqueue_struct *wq;
};
```

`struct evl_proxy` 多了一个指针：

```c
struct evl_proxy {
	struct file *filp;            /* 只持有引用，给 bridge 用 */
	struct proxy_out output;
	struct proxy_in input;        /* ring + reqsz/on_eof/on_error，没变 */
	struct proxy_bridge *bridge;  /* 新增：Linux 侧，对 EVL 不透明 */
	struct evl_element element;
	struct evl_poll_head poll_head;
};
```

**Linux 侧**（`proxy_bridge.c:24-39`）：

```c
struct proxy_relay {              /* 每个方向一个 */
	void *bufmem;                 /* 同一块数据区 */
	unsigned int bufsz;           /* 快照，不变 */
	unsigned int granularity;     /* 快照，不变 */
	unsigned int off;             /* 转发者的下标：输出方向是 rdoff，输入方向是 wroff */
	struct mutex worker_lock;
	wait_queue_head_t inband_wait;
};

struct proxy_bridge {             /* 每个 proxy 一个 */
	struct evl_proxy *proxy;      /* 不透明，只能传给门 */
	struct file *filp;            /* 目标文件 */
	bool has_out, has_in;         /* 快照：方向 */
	struct proxy_relay out, in;
};
```

### 1.3 每个字段去了哪里

| 原字段 | 现在在哪 | 为什么 |
|---|---|---|
| `bufmem` | 两边各存一个指针，指向同一块内存 | 数据区本来就是两边交换数据用的 |
| `bufsz`、`granularity` | 两边各存一份 | 创建后不变，存快照就行（doc 13 §11 第一类） |
| `fillsz` | EVL 侧 | oob 读写者要用。转发者只能经门读、经门改 |
| 输出 `wroff` / 输入 `rdoff` | EVL 侧 `proxy_ring` 的 union | EVL 写者、读者自己的下标，受 `ring->lock` 保护 |
| 输出 `rdoff` / 输入 `wroff` | Linux 侧 `proxy_relay.off` | 只有转发者用，受 `worker_lock` 保护 |
| `reserved`、`nesting`、`lock` | EVL 侧 | EVL 写者、读者之间的预留协议 |
| `oob_wait` | EVL 侧 | EVL 等待对象。转发者只能经门唤醒 |
| `relay_work`、`wq` | EVL 侧 | oob 上用它们来发起转发（`evl_call_inband_from`），见 §1.4 |
| `inband_wait_r` / `inband_wait_w` | Linux 侧 `proxy_relay.inband_wait`，合并成一个 | 输出 ring 只用 `_w`，输入 ring 只用 `_r`，另一个从来没用过 |
| `worker_lock` | Linux 侧 | 只有转发者用。它本来就是 inband 锁，现在放到了 inband 那一边 |
| `reqsz`、`on_eof`、`on_error` | EVL 侧，`struct proxy_in` 不变 | oob 读者要用。转发者只能经门改 |
| `filp` | EVL 侧持有引用，Linux 侧存一份指针 | 见 §1.4 |

### 1.4 三个容易有疑问的地方

1. **`wq` 为什么留在 EVL 侧？**
   `wq` 是 Linux 的 workqueue，但真正用它的只有一处：oob 写者、读者调用 `evl_call_inband_from(&ring->relay_work, ring->wq)`，请求"到 inband 去跑一次转发"。转发者自己从来不用它，inband 入口也是直接调用转发者。所以它是 EVL 发起转发时用的一个句柄，创建后不变，留在 EVL 侧。
2. **`filp` 放在哪？**
   引用计数还是 EVL 工厂管：build 时 `fget`，dispose 时 `fput`，和原来一样，错误码的顺序也不变。bridge 只存一份指针，用来读写目标文件。EVL 侧从来不解引用它。
3. **`wroff`/`rdoff` 为什么用 union，而不是改名？**
   EVL 侧每个 ring 只剩一个下标。用 union 的话，`do_proxy_write` 里的 `ring->wroff`、`do_proxy_read` 里的 `ring->rdoff` 一个字都不用改，diff 最小，而且读起来仍然是"写者写到哪、读者读到哪"。

### 1.5 门是什么

门就是 `proxy.c` 里的普通函数，在 `proxy_gate.h` 里声明。每个门对应原来转发者里直接操作 EVL 状态的一两条语句。门的参数是不透明的 `struct evl_proxy *`，方向写在函数名里（`out_` / `in_`）。

| 门 | 原来的语句 | 用在哪 |
|---|---|---|
| `proxy_gate_bridge(filp)` | `element_of(filp, ...)`，再取出 `bridge` | inband 入口：从文件找到 bridge |
| `proxy_gate_write(filp, buf, n, &kick)` | 原 `do_proxy_write` | oob 写者、inband 写者 |
| `proxy_gate_read(filp, buf, n)` | 原 `do_proxy_read`（只改了名字） | oob 读者、inband 读者 |
| `proxy_gate_can_write(proxy, n)` | `can_write_buffer(ring, n)` | inband 写者的等待条件 |
| `proxy_gate_out_fill(proxy)` | `atomic_read(&ring->fillsz)` | `relay_output`、`proxy_bridge_poll` |
| `proxy_gate_out_consume(proxy, n)` | `atomic_sub_return(n, &ring->fillsz)` | `relay_output` |
| `proxy_gate_out_done(proxy, remaining)` | `relay_output` 结尾的 EVL 通知 | `relay_output` |
| `proxy_gate_in_fill(proxy)` | `atomic_read(&ring->fillsz)` | `proxy_bridge_poll` |
| `proxy_gate_in_request(proxy, n)` | `atomic_add(n, &in->reqsz)` | inband 读者 |
| `proxy_gate_in_requested(proxy)` | `atomic_read(&in->reqsz)` | `relay_input` |
| `proxy_gate_in_produce(proxy, n)` | `atomic_add(n, &ring->fillsz)` | `relay_input` |
| `proxy_gate_in_fulfil(proxy, n)` | `atomic_sub_return(n, &in->reqsz)` | `relay_input` |
| `proxy_gate_in_stop(proxy, done, ret)` | 出错或 EOF 时那几行 | `relay_input` |
| `proxy_gate_in_done(proxy, exception)` | `relay_input` 结尾的 EVL 通知 | `relay_input` |
| `proxy_gate_in_take_eof(proxy)` | `atomic_cmpxchg(&in->on_eof, true, false)` | inband 读者 |

修改计数的门（`out_consume`、`in_produce`、`in_fulfil`、`in_stop`）会检查参数，检查失败用 `EVL_WARN_ON_ONCE(CORE, ...)` 报警，然后照常执行。所以行为不变，只在打开 `CONFIG_EVL_DEBUG_CORE` 时多一条警告。例如 `in_produce` 检查 `fillsz + n <= bufsz`，P5 一旦发生就会报出来。

反方向，`proxy.c` 调用 `proxy_bridge.c` 的函数也在 `proxy_gate.h` 里声明：`proxy_bridge_create` / `destroy`、`proxy_bridge_relay_output` / `relay_input`、4 个 inband 文件操作。这个方向是"可信调用非可信"，不需要校验。

下面每节讲一个函数。

---

## 2. `do_proxy_write` → 门 `proxy_gate_write`（`proxy.c:111`）

**它做什么**：把用户数据拷进输出 ring。持 `ring->lock` 预留空间，放锁拷贝，最后一个写者把预留量发布到 `fillsz`。

**原来的结尾**：

```c
	if (--ring->nesting == 0) {
		n = atomic_add_return(ring->reserved, &ring->fillsz);
		rsvd = ring->reserved;
		ring->reserved = 0;
		if (n == rsvd) { /* empty -> non-empty transition */
			if (running_inband()) {                      /* inband 调用者 */
				raw_spin_unlock_irqrestore(&ring->lock, flags);
				relay_output(proxy);                     /* 直接跑转发者 */
				return ret;
			}
			evl_call_inband_from(&ring->relay_work, ring->wq);  /* oob 调用者 */
		}
	}
```

**现在**：

```c
	*kick = false;                                       /* 函数开头 */
	...
	if (--ring->nesting == 0) {
		...
		if (n == rsvd) /* empty -> non-empty transition */
			*kick = true;
	}
```

**改了什么**：不再自己发起转发，只告诉调用者"ring 由空变非空了"（`*kick = true`），由调用者决定怎么发起。

**为什么**：原来这里是一个 EVL 函数里面直接调用 Linux 侧的 `relay_output`。拆分以后 `relay_output` 在另一个文件里，EVL 的门不应该替调用者决定去 inband 跑 Linux 代码。每个调用者都知道自己是谁：

- oob 入口（§3）：挂一个 `relay_work`；
- inband 入口（§10）：直接调转发者。

**用到的结构体字段**：全都在 EVL 侧（`lock`、`wroff`、`nesting`、`reserved`、`fillsz`、`bufmem`），一个都没变。

---

## 3. `proxy_oob_write`（`proxy.c:282`，EVL 侧）

**原来**：

```c
	do {
		ret = do_proxy_write(filp, u_buf, count);
		if (ret != -EAGAIN || proxy_nonblock(filp))
			break;
		ret = evl_wait_flag(&ring->oob_wait);
	} while (!ret);
```

**现在**：

```c
	do {
		ret = proxy_gate_write(filp, u_buf, count, &kick);
		if (kick) {
			if (running_inband()) {
				proxy_bridge_relay_output(proxy->bridge);
				return ret;
			}
			evl_call_inband_from(&ring->relay_work, ring->wq);
		}
		if (ret != -EAGAIN || proxy_nonblock(filp))
			break;
		ret = evl_wait_flag(&ring->oob_wait);
	} while (!ret);
```

**改了什么**：原来藏在 `do_proxy_write` 里的"发起转发"挪到了这里。

**为什么这里还有 `running_inband()`**：我原本以为 `oob_write` 一定跑在 oob 上，查了 `syscall.c` 才发现不是这样：

- 非 EVL 线程调用 `oob_write` 时，`do_inband_syscall` 会先调 `evl_switch_oob()`，它对非 EVL 线程返回 `-EPERM`（`sched/core.c:1052`）；
- 但调用者只把 `-ERESTARTSYS` 当作失败（`syscall.c:405`），所以接着在 inband 上执行 `proxy_oob_write`。

原代码的 `running_inband()` 分支就是为这种情况写的：同步转发，在调用者自己的上下文里把数据写出去。为了保持这个行为，分支保留了。

它和原来的区别是：现在 inband 分支里只有一句"调用 Linux 侧函数"，也就是我们之前说的"把 inband 代码块抽成一个函数"。真正干活的代码都在 `proxy_bridge.c` 里。

**一处时序变化**：`evl_call_inband_from` 原来在 `ring->lock` 里面调用，现在在锁外面（门已经放锁了）。这没有影响，见 §17。

---

## 4. `relay_output`（`proxy_bridge.c:41`，Linux 侧）

**它做什么**：输出方向的转发者。在 inband 上把输出 ring 里已发布的数据用 `kernel_write` 写到目标文件，然后唤醒等着腾出空间的写者。

**逐处对照**（只列变化的行；分块计算、`f_pos` 处理、`kernel_write` 都没变）：

| 原来 | 现在 | 涉及的字段 |
|---|---|---|
| `relay_output(struct evl_proxy *proxy)` | `relay_output(struct proxy_bridge *br)` | 参数换成 Linux 侧对象 |
| `struct proxy_ring *ring = &proxy->output.ring;` | `struct proxy_relay *rl = &br->out;` | |
| `filp = proxy->filp` | `filp = br->filp` | `filp` 的指针在 bridge 里 |
| `mutex_lock(&ring->worker_lock)` | `mutex_lock(&rl->worker_lock)` | `worker_lock` 搬到 Linux 侧 |
| `count = atomic_read(&ring->fillsz)` | `count = proxy_gate_out_fill(br->proxy)` | `fillsz` 在 EVL 侧，经门读 |
| `rdoff = ring->rdoff` | `rdoff = rl->off` | 转发者的下标搬到 Linux 侧 |
| `ring->bufsz`、`ring->granularity`、`ring->bufmem` | `rl->bufsz`、`rl->granularity`、`rl->bufmem` | 快照 |
| `count = atomic_sub_return(count, &ring->fillsz)` | `count = proxy_gate_out_consume(br->proxy, count)` | 经门消费 |
| `ring->rdoff = rdoff` | `rl->off = rdoff` | |
| 结尾的通知（见下） | `proxy_gate_out_done(br->proxy, count)` + `wake_up(&rl->inband_wait)` | |

**结尾的通知**，原来是：

```c
	if (count == 0)
		evl_signal_poll_events(&proxy->poll_head, POLLOUT|POLLWRNORM);   /* EVL */
	if (count < ring->bufsz) {
		evl_raise_flag(&ring->oob_wait);   /* EVL：先唤醒 oob 写者 */
		wake_up(&ring->inband_wait_w);     /* Linux：再唤醒 inband 写者 */
	} else {
		evl_schedule();                    /* EVL */
	}
```

现在分成两半：EVL 的部分进了门 `proxy_gate_out_done`（`proxy.c:426`，代码原样搬过去），Linux 的部分留在转发者里：

```c
	proxy_gate_out_done(br->proxy, count);   /* poll 事件、oob_wait、evl_schedule */
	if (count < rl->bufsz)
		wake_up(&rl->inband_wait);
```

顺序不变：先唤醒 oob，再唤醒 inband，保留原注释"give precedence to oob waiters"的语义。

**为什么这样切**：转发者真正需要 EVL 做的只有三件事：告诉我有多少数据（`out_fill`）、我消费了多少（`out_consume`）、我做完了（`out_done`）。其余都是它自己的 Linux I/O。

**改完以后**：`relay_output` 里一个 `evl_*` 调用都没有，也碰不到 `oob_wait`、`poll_head`。

---

## 5. `relay_output_work`（`proxy.c:92`，EVL 侧）

**它做什么**：`relay_work` 的回调。oob 写者挂了 work 以后，kworker 在 inband 上调用它。

**原来**：`container_of` 找到 proxy，再调 `relay_output(proxy)`。
**现在**：`container_of` 找到 proxy，再调 `proxy_bridge_relay_output(proxy->bridge)`。

**为什么留在 `proxy.c`**：`relay_work` 是 EVL 侧的字段，只有 `proxy.c` 能从它 `container_of` 出 `evl_proxy`。回调从 EVL 侧取出 `bridge` 指针，转交给 Linux 侧，这是可信调用非可信的方向。

`relay_input_work`（`proxy.c:180`）同理。

---

## 6. `relay_input`（`proxy_bridge.c:101`，Linux 侧）

**它做什么**：输入方向的转发者。按读者请求的量（`reqsz`），用 `kernel_read` 从目标文件读进输入 ring，发布给读者；读到 EOF 或出错就记下来；最后唤醒读者。

**逐处对照**：

| 原来 | 现在 | 涉及的字段 |
|---|---|---|
| `mutex_lock(&ring->worker_lock)` | `mutex_lock(&rl->worker_lock)` | Linux 侧 |
| `count = atomic_read(&in->reqsz)` | `count = proxy_gate_in_requested(br->proxy)` | `reqsz` 在 EVL 侧 |
| `wroff = ring->wroff` | `wroff = rl->off` | 转发者的下标，Linux 侧 |
| 出错 / EOF 时：`atomic_sub(count - len, &in->reqsz); if (ret) in->on_error = ret; else atomic_set(&in->on_eof, true);` | `proxy_gate_in_stop(br->proxy, count - len, ret)` | 三个 EVL 字段，一个门 |
| `atomic_add(ret, &ring->fillsz)` | `proxy_gate_in_produce(br->proxy, ret)` | 经门发布 |
| `count = atomic_sub_return(count, &in->reqsz)` | `count = proxy_gate_in_fulfil(br->proxy, count)` | |
| `ring->wroff = wroff` | `rl->off = wroff` | |
| 结尾的通知（见下） | `if (proxy_gate_in_done(br->proxy, exception)) wake_up(&rl->inband_wait);` | |

**结尾的通知**，原来是：

```c
	if (atomic_read(&ring->fillsz) > 0 || exception) {
		evl_signal_poll_events(&proxy->poll_head, POLLIN|POLLRDNORM);
		evl_raise_flag(&ring->oob_wait);
		wake_up(&ring->inband_wait_r);
	}
```

条件要读 `fillsz`，这是 EVL 的字段。所以条件判断和两个 EVL 通知一起放进门 `proxy_gate_in_done`（`proxy.c:497`），门返回"条件是否成立"，转发者据此决定要不要 `wake_up`。顺序仍然是先 oob 后 inband。

**`in_stop` 放在原位置调用**：EOF / 错误的记录仍在 `goto done` 之前，也就是放 `f_pos_lock` 和 `worker_lock` 之前，和原来的时机完全一样。

**P4 原样保留**：`wroff = (wroff + n) % rl->bufsz` 仍然按 `n` 前进，而发布的是 `ret`。这一步不修。

---

## 7. `do_proxy_read` → 门 `proxy_gate_read`（`proxy.c:192`）

只改了名字（去掉 `static`，名字改成门的前缀），函数体一个字没动。它只碰 EVL 侧字段（`lock`、`rdoff`、`reserved`、`nesting`、`fillsz`、`bufmem`、`on_error`），oob 读者和 inband 读者都调用它。

---

## 8. `proxy_oob_read`（`proxy.c:318`，EVL 侧）

只把 `do_proxy_read` 换成 `proxy_gate_read`。它在 EVL 侧，仍然直接访问 `in->reqsz`、`in->on_eof`，不需要经过门。

---

## 9. `proxy_oob_poll`、`proxy_release`

没有改。它们在 EVL 侧，只碰 EVL 侧字段。

> → 见 [doc 14 §12](../14-trust-domain-vs-stage.md) [C-05]

---

## 10. `proxy_write` → `proxy_bridge_write`（`proxy_bridge.c:175`，Linux 侧）

**它做什么**：inband 的 `write(2)`。

**原来**（在 `proxy.c` 里）：

```c
	struct evl_proxy *proxy = element_of(filp, struct evl_proxy);
	struct proxy_ring *ring = &proxy->output.ring;

	if (!proxy_may_write(proxy))
		return -ENXIO;
	do {
		ret = do_proxy_write(filp, u_buf, count);    /* 里面可能同步跑 relay_output */
		if (ret != -EAGAIN || filp->f_flags & O_NONBLOCK)
			break;
		ret = wait_event_interruptible(ring->inband_wait_w,
					can_write_buffer(ring, count));
	} while (!ret);
```

**现在**：

```c
	struct proxy_bridge *br = proxy_gate_bridge(filp);

	if (!br->has_out)
		return -ENXIO;
	do {
		ret = proxy_gate_write(filp, u_buf, count, &kick);
		if (kick) {
			relay_output(br);        /* 原来 do_proxy_write 里的 running_inband() 分支 */
			return ret;
		}
		if (ret != -EAGAIN || filp->f_flags & O_NONBLOCK)
			break;
		ret = wait_event_interruptible(br->out.inband_wait,
				proxy_gate_can_write(br->proxy, count));
	} while (!ret);
```

**改了什么**：

| 原来 | 现在 | 原因 |
|---|---|---|
| `element_of` 取 `evl_proxy` | 门 `proxy_gate_bridge` 取 bridge | Linux 侧看不到 `evl_proxy` 的定义 |
| `proxy_may_write(proxy)`（读 `element.clone_flags`） | `br->has_out` | 方向在创建后不变，存快照 |
| `do_proxy_write` 内部同步转发 | 门返回 `kick`，这里调 `relay_output` | §2 |
| 等待队列 `ring->inband_wait_w` | `br->out.inband_wait` | 搬到 Linux 侧 |
| 等待条件 `can_write_buffer(ring, ...)` | 门 `proxy_gate_can_write` | 条件要读 `fillsz`、`reserved` |

`filp->f_flags & O_NONBLOCK` 没改：这里是 inband，可以直接读 Linux 的字段。

**为什么 `kick` 以后直接 `return`**：原来 `do_proxy_write` 在走 `running_inband()` 分支时，跑完 `relay_output` 就 `return ret`，外面的循环看到 `ret != -EAGAIN` 也就退出了。`kick` 只会在预留成功以后出现，这时 `ret` 不可能是 `-EAGAIN`，所以直接返回和原来等价。

---

## 11. `proxy_read` → `proxy_bridge_read`（`proxy_bridge.c:205`，Linux 侧）

**它做什么**：inband 的 `read(2)`。ring 里不够就登记请求，在自己的上下文里同步跑一次 `relay_input`，再重试。

| 原来 | 现在 |
|---|---|
| `element_of` + `proxy_may_read` | `proxy_gate_bridge` + `br->has_in` |
| `do_proxy_read(filp, ...)` | `proxy_gate_read(filp, ...)` |
| `atomic_add(count, &in->reqsz)` | `proxy_gate_in_request(br->proxy, count)` |
| `relay_input(proxy)` | `relay_input(br)` |
| `atomic_cmpxchg(&in->on_eof, true, false)` | `proxy_gate_in_take_eof(br->proxy)` |

循环结构和判断条件一个没变。

---

## 12. `proxy_poll` → `proxy_bridge_poll`（`proxy_bridge.c:236`，Linux 侧）

| 原来 | 现在 |
|---|---|
| `poll_wait(filp, &oring->inband_wait_w, wait)` | `poll_wait(filp, &br->out.inband_wait, wait)` |
| `atomic_read(&oring->fillsz) < oring->bufsz` | `proxy_gate_out_fill(br->proxy) < br->out.bufsz` |
| `poll_wait(filp, &iring->inband_wait_r, wait)` | `poll_wait(filp, &br->in.inband_wait, wait)` |
| `atomic_read(&iring->fillsz) > 0` | `proxy_gate_in_fill(br->proxy) > 0` |
| `proxy->filp->f_op->poll(...)` | `br->filp->f_op->poll(...)`（合并目标文件的事件，和原来一样） |

**等待队列合并以后还对吗**：原来 poll 登记在输出 ring 的 `_w` 和输入 ring 的 `_r` 上，转发者也只唤醒这两个。现在这两个分别就是 `br->out.inband_wait` 和 `br->in.inband_wait`，登记和唤醒是一一对应的。

---

## 13. `proxy_mmap` → `proxy_bridge_mmap`（`proxy_bridge.c:259`，Linux 侧）

只把 `proxy->filp` 换成 `proxy_gate_bridge(filp)->filp`。

注意，只用来 mmap、没有方向的 proxy（`bufsz == 0`）也会创建 bridge（`has_out`、`has_in` 都是 false），所以这里总能拿到 bridge。

---

## 14. `init_ring`（`proxy.c:540`，EVL 侧）

删掉三行：`init_waitqueue_head(&ring->inband_wait_r)`、`init_waitqueue_head(&ring->inband_wait_w)`、`mutex_init(&ring->worker_lock)`。这三个字段已经不在 `proxy_ring` 里了，由 `proxy_bridge_create` 初始化。分配 `bufmem`、`wq`，初始化 `lock`、`relay_work`、`oob_wait` 都没变。

---

## 15. `proxy_factory_build` / `proxy_factory_dispose`（`proxy.c:589`、`:687`）

**build**：两个 ring 初始化完以后，新增：

```c
	proxy->bridge = proxy_bridge_create(proxy, filp,
				clone_flags & EVL_CLONE_OUTPUT ? proxy->output.ring.bufmem : NULL,
				clone_flags & EVL_CLONE_INPUT  ? proxy->input.ring.bufmem  : NULL,
				bufsz, attrs.granularity);
	if (proxy->bridge == NULL) {
		ret = -ENOMEM;
		goto fail_bridge;     /* 新标签：先销毁输入 ring，再走原来的清理链 */
	}
```

`proxy_bridge_create`（`proxy_bridge.c:300`）分配 bridge，存下 `proxy`、`filp`，然后对每个有效的方向填 `bufmem`/`bufsz`/`granularity` 快照，`off = 0`，初始化 `worker_lock` 和 `inband_wait`。传 `NULL` 表示没有这个方向。

**为什么放在最后**：bridge 要拿到 `bufmem` 指针，所以必须在 ring 分配之后。build 返回之前，没有人能打开这个 proxy，也就不会有 work 被挂上，所以 `proxy->bridge` 最后才赋值也没问题。

**dispose**：两个 ring 销毁以后、`fput` 之前，新增 `proxy_bridge_destroy(proxy->bridge)`。顺序很重要：`destroy_ring` 里的 `evl_cancel_work` 保证不会再有转发者运行；文件已经 release，inband 入口也不会再进来。之后 bridge 就没人用了，再释放它。

---

## 16. Makefile

`kernel/evl/Makefile` 的 `evl-y` 里加 `proxy_bridge.o`。

---

## 17. 行为有没有变

| 方面 | 结论 |
|---|---|
| 每个门 | 就是原来的那一两条语句，原子操作的种类和顺序都没变 |
| 锁 | `ring->lock`、`worker_lock`、`f_pos_lock` 的加锁范围不变，只是 `worker_lock` 换了位置 |
| 唤醒顺序 | 先 oob（门里），再 inband（Linux 侧） |
| EOF / 错误的记录时机 | 不变（§6） |
| inband 写者同步转发 | 不变（§10） |
| 非 EVL 线程调 `oob_write` 时同步转发 | 不变（§3） |
| oob 写者挂 work | **有一处时序变化**：`evl_call_inband_from` 从 `ring->lock` 里面挪到外面 |
| P1–P6 | 全部原样保留 |
| 门的校验 | 只在 `CONFIG_EVL_DEBUG_CORE` 下报警，不改变执行 |

**那处时序变化为什么没影响**：`kick` 只在 ring 由空变非空时出现。从放锁到挂 work 之间，可能发生下面两种情况：

- 别的 oob 写者又写了一些：ring 已经非空，它不会再挂 work，等这次的 work 一起转发。
- 上一轮的转发者还没退出：它已经把 `fillsz` 减到 0，正准备退出，这次挂的 work 会再跑一轮。

挂 work 本身由 `evl_work` 的 PENDING 位去重（03），挂早挂晚、重复挂都安全。最坏的结果只是转发晚开始几条指令。

---

## 18. 验证

| 项目 | 结果 |
|---|---|
| 编译 `build/linux-evl-qemu`、`build/linux-evl-full`，`W=1` | `proxy.o`、`proxy_bridge.o` 都通过，没有警告 |
| 边界由编译器保证 | 在 `proxy_bridge.c` 里加 `br->proxy->bridge`，报 `invalid use of undefined type 'struct evl_proxy'`。验证后已删除 |
| `proxy_bridge.o` 的外部符号 | EVL 符号只有 15 个 `proxy_gate_*`，没有任何 `evl_*`。其余都是 Linux 的：`kernel_read/write`、`mutex_*`、`__wake_up`、`prepare_to_wait_event`、`kmalloc`、`fput` 等 |
| `proxy.o` 的外部符号 | 不再有 `kernel_read`、`kernel_write`、`mutex_lock`、`__wake_up`。新增 8 个 `proxy_bridge_*` |
| 链接 bzImage、跑 libevl 的 proxy 测试 | **未做**，和 01–03 一起放在待办里 |

---

## 19. 和 04 设计稿的差别

实现时我替你选了 04 §10 待确认问题的答案，并做了几处简化：

| 04 §10 的问题，或 04 原来的写法 | 实现 | 理由 |
|---|---|---|
| Q1：两个方向都改？ | 都改 | 两个方向对称，只改一半，结构体就得两套写法 |
| Q2：要不要 `proxy_bridge.c`？ | 要 | 边界由编译器保证，不靠约定 |
| Q3：保留 P1–P6？ | 保留 | 这一步只做搬迁，修 bug 单独做 |
| Q4：`filp` 放哪？ | EVL 管引用，bridge 存指针 | build/dispose 的错误码和顺序都不变 |
| Q5：work 回调怎么找到 bridge？ | 回调留在 `proxy.c`，经 `proxy->bridge` 转交 | 不用在 ring 里再存一个指针 |
| Q6：门的前缀？ | `proxy_gate_*`，反方向用 `proxy_bridge_*` | — |
| Q7：P6 是不是有意的？ | 未定，保持原样 | — |
| 04：门的参数是 `struct proxy_ring *` | 改成 `struct evl_proxy *`，方向写在函数名里 | 只有一个句柄；输入方向的门还要碰 `reqsz` 等字段，用 ring 的话还得 `container_of` |
| 04：`wq` 放在 Linux 侧，再给 ring 存快照 | 只放在 EVL 侧 | 只有 EVL 用它（§1.4） |
| 04：拆出 `struct proxy_in_state` | 不拆，`struct proxy_in` 不变 | 这三个字段本来就都在 EVL 侧，拆它不解决问题 |
| 04：`evl_off` 改名 | 用 union 保留 `wroff`/`rdoff` | `do_proxy_write/read` 不用改 |
| 04：错误/EOF 用 G6 + G7 | 新增 `in_stop`，在原位置调用 | 记录时机和原来完全一致 |
| 04：G8 消掉所有 `running_inband()` | oob 入口里保留一个，只剩一行调用 | 非 EVL 线程的 `oob_write` 会跑在 inband 上（§3），04 漏了这种情况 |
