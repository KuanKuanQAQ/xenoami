# 18 xbuf 拆分方案（第二版）：可信端私有，共享区只当输入

> 2026-11-10。步骤 1–3 **已实施**（见 §16）。下文的行号指改动前的 `kernel/evl/xbuf.c`（770 行，即 HEAD `daed79c1cd9b`）。
> 依据的规则 C1–C7 见 [19](19-untrusted-peer-channels.md)。19 还用 proxy 对照了这组规则。
> **第一版已被替代**：第一版用"可信核心 + reserve/commit 门 + 不变式 I1–I5"保护环的下标，目的是保证通道正确。按 19 的思路，对端不可信时通道正确性不值得保护，所以第二版不再这样做。两版的差别见 §11，第一版的原文没有保留在仓库里。
> 相关：xbuf 的原理和逐字段分析见 [16-subsystems/xbuf.md](16-subsystems/xbuf.md)。

---

## 0. 一句话

xbuf 的两个环，每个环的两端各属于一个域。**每一端把自己的下标、预留量、`pending` 和锁留在本域**。两端之间只共享两样东西：数据缓冲区，和一个原子计数 `fillsz`（proxy 现在就是这个形状）。

可信端读到的 `fillsz` 只当作输入，先约束再用。地址只由可信端自己的下标算出来，所以 `fillsz` 被改坏，最多读到垃圾或者一直等待，不会越界。

唤醒分两个方向：
- 不可信 → 可信：经门铃 `xbuf_gate_kick`；
- 可信 → 不可信：经通知中继，可信侧只登记。

```
             可信域                                         不可信域
┌──────────────────────────────────┐              ┌──────────────────────────────────┐
│ oob 读写路径                       │              │ inband 读写路径                    │
│  xbuf_oob_write / evl_write_xbuf  │              │  xbuf_read      （ibnd 读端）      │
│   → ibnd 写端（off/rsvd/pending）  │              │  xbuf_write     （obnd 写端）      │
│  xbuf_oob_read / evl_read_xbuf    │              │  xbuf_poll / rings_show            │
│   → obnd 读端（off/rsvd/pending）  │              │  各端自己的 off/rsvd/pending、锁     │
│  锁：ibnd.lock、obnd.i_event 的锁  │              │  rd_wait / wr_wait（Linux 等待队列）│
│  evl_flag、evl_wait_queue、poll    │◀── 门铃 ─────│  提交后 xbuf_gate_kick(h, dir, hint)│
│  通知登记 ────────────────────────│── 通知中继 ─▶│  合成中断处理函数：take → wake_up  │
└──────────────────────────────────┘              └──────────────────────────────────┘
          共享区：bufmem × 2、fillsz × 2、等待提示 × 2（两侧都能读写，内容和数值都不受保护）
```

文中的"读写路径"指原来 `do_xbuf_read` / `do_xbuf_write` 的外层循环（预留 → 不够就等 → 拷贝 → 提交）。拆分后按端分成两份，这就是之前讨论的"`do_xbuf_read` 写两份"。

---

## 1. 现状

### 1.1 两个环

| 环 | 写端 | 读端 |
|---|---|---|
| `ibnd.ring`（inbound：流进 Linux） | `xbuf_oob_write`（`:508-519`）；`evl_write_xbuf`（`:611-624`，唯一用户是 latmus，在 oob 定时器回调里以 `O_NONBLOCK` 调用，`drivers/evl/latmus.c:154`） | `xbuf_read`（inband VFS，`:368-379`） |
| `obnd.ring`（outbound：从 Linux 流出） | `xbuf_write`（inband VFS，`:381-392`） | `xbuf_oob_read`（`:495-506`）；`evl_read_xbuf`（`:595-608`，树内没有用户） |

**每个环的每一端都只属于一个域**：ibnd 的写端和 obnd 的读端是可信代码，另外两端是 Linux VFS 进来的代码。这一点是本方案的前提。proxy 不满足这一点，见 19 §3.4。

### 1.2 读写协议

详见 16/xbuf.md §2.1。每次读写分三步：
1. 持锁预留：推进本端的下标，累计预留量，`pending++`；
2. 放锁拷贝；
3. 持锁提交：`--pending`，最后一个提交的人把累计预留量发布到 `fillsz`，然后通知对端。

读：`:101-203`。写：`:205-292`。

### 1.3 回调

stage 相关的部分都在 `ring` 的 6 个回调里（`:23-39`，在 `:681-699` 设置）：

| 回调 | ibnd（oob 写，inband 读） | obnd（inband 写，oob 读） |
|---|---|---|
| `lock` / `unlock` | `ibnd.lock`（`:294-309`） | `obnd.i_event.wchan.lock`（`:434-449`） |
| `wait_input` | `inbound_wait_input`：`evl_wait_flag_head(&ibnd->o_event)` 判断短读（`:327`），否则 `wait_event_interruptible`（`:333`） | `outbound_wait_input`：`wq_has_sleeper` 判断短读（`:456`），否则 `evl_wait_event`（`:459`） |
| `signal_input` | `irq_work_queue`（`:348`）→ `resume_inband_reader` → `wake_up` | POLLIN + `evl_flush_wait_locked`（`:463-471`） |
| `wait_output` | `evl_wait_flag`（`:351-356`） | `wait_event_interruptible`（`:477-478`） |
| `signal_output` | POLLOUT + `evl_raise_flag_nosched`（`:358-366`） | `irq_work_queue`（`:492`）→ `resume_inband_writer` → `wake_up` |

### 1.4 问题 Q1–Q7

| # | 问题 | 位置 |
|---|---|---|
| Q1 | inband 代码改环的下标和 `fillsz`，oob 代码再用这些值算地址 | 写：`:157-159`、`:191`、`:245-247`、`:280`；用：`:176`、`:265` |
| Q2 | inband 代码持有 hard spinlock，oob 也要拿同一把锁 | 经 `xbuf_read` / `xbuf_write` / `xbuf_poll` 进入 |
| Q3 | oob 上调用 Linux 的 `irq_work_queue` | `:348`、`:492` |
| Q4 | oob 上读 Linux 的等待队列 | `wq_has_sleeper`，`:456` |
| Q5 | 可信对象里嵌着 Linux 对象 | `ibnd.i_event`、`obnd.o_event`（`wait_queue_head`），两个 `irq_work` |
| Q6 | 由 Linux 字段得到可信指针 | `element_of(filp)`；`evl_get_xbuf` 比较 `f_op`（`:580`） |
| Q7 | 由 Linux 字段决定行为 | `filp->f_flags & O_NONBLOCK`（`:378`、`:391`、`:505`、`:518`） |

---

## 2. 按 19 的规则重新看 Q1–Q7

| # | 第一版的看法 | 第二版的看法 | 处理 | 规则 |
|---|---|---|---|---|
| Q1 | 下标要保护，inband 只能经门改 | 只有**对端的**下标会被不可信方改。可信端的下标本来就只有可信代码写，问题在于两端共用一个 `xbuf_ring`。按端分开以后，可信端的地址只用自己的 `off` 算。`fillsz` 是两端共同修改的，可信端读到后约束到 `[0, bufsz]` | 分端（§3），`fill_of`（§4） | C2、C3 |
| Q2 | 门内取锁 | 按端分开以后，每端只拿本域的锁，**跨域不再共用锁** | 分端 | C4 |
| Q3 | 通知中继 | 不变 | §7.4 | C6 |
| Q4 | 共享计数 `ib_writers` | 不变，它只是提示 | §7.3 | C3 |
| Q5 | Linux 对象移到不可信结构 | 不变 | §3 | C7 |
| Q6、Q7 | 依赖 `evl_element` 拆分 | 不变 | §10 | — |

---

## 3. 数据划分

```c
/* ---------------- 共享区：从共享区分配器分配（19 §5），两侧都能读写 ---------------- */

struct xbuf_shared {
	atomic_t ibnd_fill;        /* ibnd.fillsz：可信写端加，不可信读端减 */
	atomic_t obnd_fill;        /* obnd.fillsz：不可信写端加，可信读端减 */
	atomic_t oob_writers;      /* 提示：在 ibnd 上等空间的 oob 写者数，可信侧写（§7.3） */
	atomic_t ib_writers;       /* 提示：在 obnd 上等空间的 inband 写者数，不可信侧写 */
};
/* 两块 bufmem 也从共享区分配 */

/* ---------------- 可信：只在 xbuf.c 里可见 ---------------- */

struct xbuf_end {                  /* 一个环的一端，读端和写端用同一个结构 */
	void *bufmem;              /* 可信副本，创建后不变 */
	unsigned int bufsz;        /* 可信副本，创建后不变（≤ 2^30，:643-647） */
	atomic_t *fill;            /* 指向共享区的 fillsz；指针本身是可信的 */
	unsigned int off;          /* 原 rdoff 或 wroff */
	unsigned int rsvd;         /* 原 rdrsvd 或 wrrsvd */
	int pending;               /* 原 rdpending 或 wrpending */
};

struct evl_xbuf {
	struct evl_element element;          /* 拆分见 §10 */
	struct {                             /* oob 写 → inband 读 */
		struct xbuf_end w;           /* 可信写端 */
		hard_spinlock_t lock;
		struct evl_flag o_event;     /* oob 写者等空间 */
		struct evl_inband_notify notify;   /* 叫醒 inband 读者（§7.4） */
	} ibnd;
	struct {                             /* inband 写 → oob 读 */
		struct xbuf_end r;           /* 可信读端；锁就是 i_event 的锁 */
		struct evl_wait_queue i_event;     /* oob 读者等数据 */
		struct evl_inband_notify notify;   /* 叫醒 inband 写者 */
	} obnd;
	struct evl_poll_head poll_head;
	struct xbuf_shared *sh;
	void *ib_cookie;                     /* 不可信侧的 xbuf_inband *，只原样交还（C7） */
};

/* ---------------- 不可信：只在 xbuf_inband.c 里可见 ---------------- */

struct xbuf_inband {
	xbuf_handle_t h;                     /* 只能交给门 */
	struct xbuf_shared *sh;
	struct xbuf_end r;                   /* ibnd 读端（不可信侧自己的一份定义） */
	struct xbuf_end w;                   /* obnd 写端 */
	spinlock_t rlock, wlock;             /* Linux 锁，只有 inband 读者 / 写者拿 */
	wait_queue_head_t rd_wait;           /* 原 ibnd.i_event */
	wait_queue_head_t wr_wait;           /* 原 obnd.o_event */
};
```

### 3.1 旧字段到新位置

| 旧字段 | 新位置 | 理由 |
|---|---|---|
| `ring.bufmem` 的内容 | 共享区 | 只被拷贝，被改坏只会让数据变成垃圾（C1） |
| `ring.bufmem` 指针、`bufsz` | 两端各存一份 | 可信端只用自己那份算地址（C2） |
| `ring.fillsz` | 共享区，改成 `atomic_t` | 两端共同修改。原来由跨域的锁保护，锁拆开以后只能用原子操作，这和 proxy 的做法一样 |
| `ring.rdoff`、`rdrsvd`、`rdpending` | 读端私有 | ibnd 的在不可信侧，obnd 的在可信侧 |
| `ring.wroff`、`wrrsvd`、`wrpending` | 写端私有 | ibnd 的在可信侧，obnd 的在不可信侧 |
| 6 个回调 | 删除 | 每一端只属于一个 stage，等待和通知的方式写死在这一端的读写路径里 |
| `ibnd.lock` | 可信 | 只剩可信写者拿 |
| `obnd.i_event`（含它的锁） | 可信 | 只剩可信读者拿 |
| `ibnd.o_event`（`evl_flag`） | 可信 | 不变 |
| `ibnd.i_event`、`obnd.o_event`（`wait_queue_head`） | 不可信 `rd_wait` / `wr_wait` | Q5 |
| 两个 `irq_work` | 删除，改成可信的 `evl_inband_notify` | Q3、Q5 |
| （新增）`oob_writers`、`ib_writers` | 共享区 | 短读规则的提示（§7.3） |
| `poll_head` | 可信 | 只给 oob poller 用 |

---

## 4. 读共享计数

```c
/* 可信侧读共享的 fillsz：只当输入，约束到 [0, bufsz]（C3(a)） */
static inline unsigned int fill_of(atomic_t *fill, unsigned int bufsz)
{
	int v = atomic_read(fill);

	if (v < 0)
		return 0;
	return min_t(unsigned int, v, bufsz);
}
```

**为什么这样就够了**：可信端访问的地址是 `bufmem + off`，拷贝长度是 `n`。
- `bufmem`、`bufsz`、`off` 都是可信端私有的，`off < bufsz` 由取模保证；
- 一次拷贝的 `n ≤ bufsz - off`，回绕时分两段，`:163-166`、`:251-254` 的写法不变；
- 一次读写的总长度 `len`：读端 `len ≤ avail ≤ fill_of(...) ≤ bufsz`；写端 `len ≤ bufsz - fill - rsvd`，否则就等待。

所以不管共享区里的 `fillsz` 是多少，可信代码都只会访问 `[bufmem, bufmem + bufsz)`。

被改坏的后果：
- 读端看到的 `fillsz` 偏大：读到还没写进去的垃圾；
- 写端看到的 `fillsz` 偏小：覆盖对端还没读的数据；
- 写端看到的 `fillsz` 偏大：一直等待，或者拿到 `-EAGAIN`。

这些都是放弃的保证（19 §2）。

**`pending` 和 `rsvd` 是私有的**：第一版里"不可信方不提交，可信端的 `pending` 永远不归零"的问题（第一版 P2）在第二版里不存在，因为不可信方碰不到可信端的 `pending`。

---

## 5. 可信读写路径（oob）

ibnd 写端。和原来的 `do_xbuf_write` 相比，只有标 ★ 的行不同：

```c
static ssize_t xbuf_ibnd_write(struct evl_xbuf *xbuf, struct xbuf_wdesc *wd, bool nonblock)
{
	struct xbuf_end *e = &xbuf->ibnd.w;
	ssize_t len = wd->count, ret, wbytes, n;
	unsigned int wroff, fill;
	unsigned long flags;
	int xret;

	if (len == 0)
		return 0;
	if (e->bufsz == 0)
		return -ENOBUFS;
	wd->buf_ptr = wd->buf;

	for (;;) {
		raw_spin_lock_irqsave(&xbuf->ibnd.lock, flags);
		fill = fill_of(e->fill, e->bufsz);                     /* ★ */
		if (fill + e->rsvd + len > e->bufsz) {
			raw_spin_unlock_irqrestore(&xbuf->ibnd.lock, flags);
			if (nonblock)
				return -EAGAIN;
			atomic_inc(&xbuf->sh->oob_writers);            /* ★ 提示，§7.3 */
			ret = evl_wait_flag(&xbuf->ibnd.o_event);      /* 原 inbound_wait_output */
			atomic_dec(&xbuf->sh->oob_writers);            /* ★ */
			if (ret)
				return ret;
			continue;
		}
		wroff = e->off;
		e->off = (wroff + len) % e->bufsz;
		e->pending++;
		e->rsvd += len;
		wbytes = ret = len;
		do {	/* 原 :250-275，地址用 e->bufmem + wroff */
			...
		} while (wbytes > 0);
		if (--e->pending == 0) {
			atomic_add(e->rsvd, e->fill);                  /* ★ 原 :279-281 */
			e->rsvd = 0;
			evl_inband_notify_post(&xbuf->ibnd.notify);    /* ★ 原 inbound_signal_input */
		}
		raw_spin_unlock_irqrestore(&xbuf->ibnd.lock, flags);
		break;
	}
	evl_schedule();
	return ret;
}
```

obnd 读端对称，锁是 `obnd.i_event.wchan.lock`：
- `avail = fill_of(...) - rsvd`，结果小于 0 时当 0；
- 短读的提示读 `atomic_read(&xbuf->sh->ib_writers) > 0`，代替 `wq_has_sleeper`（Q4）；
- 等待用 `evl_wait_event(&xbuf->obnd.i_event, fill_of(e->fill, e->bufsz) >= len)`；
- 提交时 `atomic_sub(e->rsvd, e->fill)`，然后 `evl_inband_notify_post(&xbuf->obnd.notify)`，代替 `outbound_signal_output`。

两个 sigpoll（原来 `:190`、`:279`）在可信端提交时**不再需要**。原来可信端提交时发的是叫醒 inband 一侧的通知，那个通知不带 sigpoll，所以这里只是把计算删掉。

入口函数不变，只是改调新的路径：`xbuf_oob_write`、`evl_write_xbuf` → `xbuf_ibnd_write`；`xbuf_oob_read`、`evl_read_xbuf` → `xbuf_obnd_read`。

---

## 6. 不可信读写路径（inband）

ibnd 读端（`xbuf_read`）。它就是原来的 `do_xbuf_read` 加上 `inbound_wait_input`，改成只用本域的东西：

```c
static ssize_t xbuf_read(struct file *filp, char __user *u_buf, size_t count, loff_t *ppos)
{
	struct xbuf_inband *ib = inband_of(filp);              /* §10.1 */
	struct xbuf_end *e = &ib->r;
	...
	for (;;) {
		spin_lock_irqsave(&ib->rlock, flags);          /* Linux 锁 */
		fill = fill_of(e->fill, e->bufsz);
		avail = fill > e->rsvd ? fill - e->rsvd : 0;
		if (avail < len) {
			if (nonblock) { ... 同 :131-136 ... }
			if (len > e->bufsz) { ret = -EINVAL; break; }
			spin_unlock_irqrestore(&ib->rlock, flags);
			if (avail > 0 && atomic_read(&ib->sh->oob_writers) > 0) {
				len = avail;                   /* 短读，原 :325-331 */
				goto retry;
			}
			ret = wait_event_interruptible(ib->rd_wait,
					fill_of(e->fill, e->bufsz) >= len);
			if (ret)
				return ret;
			continue;
		}
		... 预留、放锁拷贝（copy_to_user）、取锁，同 :156-186 ...
		if (--e->pending == 0) {
			old = atomic_sub_return(e->rsvd, e->fill) + e->rsvd;
			sigpoll = old == e->bufsz;             /* 满 → 不满，原 :190 */
			e->rsvd = 0;
			kick = true;
		}
		break;
	}
	spin_unlock_irqrestore(&ib->rlock, flags);
	if (kick)
		xbuf_gate_kick(ib->h, XBUF_IBND, sigpoll);     /* 门铃，§7.2；里面会 evl_schedule */
	return ret;
}
```

obnd 写端（`xbuf_write`）对称：
- 等待前后增减 `ib->sh->ib_writers`；
- 等待用 `wait_event_interruptible(ib->wr_wait, fill + len <= bufsz)`；
- 提交时 `old = atomic_add_return(...) - rsvd`，`sigpoll = (old == 0)`，然后 `xbuf_gate_kick(ib->h, XBUF_OBND, sigpoll)`。

`xbuf_poll`：`poll_wait` 等 `rd_wait`、`wr_wait`，就绪状态直接读共享区的两个 `fillsz`（原 `:411-423`），不需要门。

`rings_show`：直接读共享区的两个 `fillsz` 和本侧的 `bufsz` 快照，不需要门。

这些代码里的任何 bug，比如 inband 一侧的预留量算错，都只影响通道本身，不扩大可信域。

---

## 7. 等待与唤醒

### 7.1 四种等待者

| 等待者 | 在哪里等 | 谁叫醒 | 怎么叫醒 |
|---|---|---|---|
| ibnd 的 oob 写者 | `ibnd.o_event`（`evl_flag`，可信） | inband 读者提交 | 门铃 `XBUF_IBND` |
| ibnd 的 inband 读者 | `rd_wait`（Linux） | oob 写者提交 | 通知中继 |
| obnd 的 oob 读者 | `obnd.i_event`（`evl_wait_queue`，可信） | inband 写者提交 | 门铃 `XBUF_OBND` |
| obnd 的 inband 写者 | `wr_wait`（Linux） | oob 读者提交 | 通知中继 |

### 7.2 门铃：不可信 → 可信

```c
void xbuf_gate_kick(xbuf_handle_t h, int dir, bool sigpoll_hint)
{
	struct evl_xbuf *xbuf = xbuf_of_handle(h);     /* 校验句柄，§10 */
	unsigned long flags;

	if (!xbuf)
		return;
	if (dir == XBUF_IBND) {                        /* 原 inbound_signal_output */
		raw_spin_lock_irqsave(&xbuf->ibnd.lock, flags);
		if (sigpoll_hint)
			evl_signal_poll_events(&xbuf->poll_head, POLLOUT|POLLWRNORM);
		evl_raise_flag_nosched(&xbuf->ibnd.o_event);
		raw_spin_unlock_irqrestore(&xbuf->ibnd.lock, flags);
	} else {                                       /* 原 outbound_signal_input */
		raw_spin_lock_irqsave(&xbuf->obnd.i_event.wchan.lock, flags);
		if (sigpoll_hint)
			evl_signal_poll_events(&xbuf->poll_head, POLLIN|POLLRDNORM);
		evl_flush_wait_locked(&xbuf->obnd.i_event, 0);
		raw_spin_unlock_irqrestore(&xbuf->obnd.i_event.wchan.lock, flags);
	}
	evl_schedule();
}
```

- 参数里只有 `sigpoll_hint` 来自不可信方。它只决定要不要多发一次 poll 事件。不可信方谎报的话，结果是 oob poller 多醒一次或者少醒一次，属于放弃的保证。
- 门铃不读也不写任何共享状态。被叫醒的 oob 等待者会自己用 `fill_of` 重新判断。
- 原来这两个动作在 inband 读写者**持有环锁**时执行（`:188-194`、`:278-283`）。现在不可信侧先放自己的锁，再敲门铃。可信侧在门内取自己的锁。顺序上的差别只在于唤醒发生在对端放锁之后，不影响结果，因为被叫醒的一方总要重新取锁再判断。
- **门铃不做去重**：起初想过可信侧记下上一次的 `fillsz`，计数没变就不唤醒。这样做有两个问题。
  - 挡不住风暴：不可信方每次都可以真的改 `fillsz`，然后再敲门铃。风暴的问题记在 19 的 U1。
  - 会丢唤醒。以 ibnd 为例：oob 写者 A 已经预留了 X 字节还没提交，写者 B 在等空间。这时 inband 读者消费了 X 字节（`fillsz` 减 X），接着 A 提交（`fillsz` 加 X），然后读者敲门铃。门铃看到的 `fillsz` 和上一次相同，于是不唤醒 B。但 A 的预留量已经从 `rsvd` 转进了 `fillsz`，B 能用的空间 `bufsz - fill - rsvd` 实际上多了 X，B 应该醒来。原来的代码每次提交都会叫醒，没有这个问题。

### 7.3 短读规则的两个提示

原来的规则：阻塞读者发现数据不够、但又不是 0，而对端有写者正在等空间时，就有多少读多少，避免两边互相等死（`:318-324` 的注释）。

| 环 | 读者 | "对端写者在等"的来源 | 新的来源 | 谁写 |
|---|---|---|---|---|
| ibnd | inband（不可信） | `evl_wait_flag_head(&ibnd->o_event)`（`:325-331`），读可信状态 | `sh->oob_writers` | 可信写者在 `evl_wait_flag` 前后增减 |
| obnd | oob（可信） | `wq_has_sleeper(&obnd->o_event)`（`:456`），读 Linux 状态 | `sh->ib_writers` | 不可信写者在等待前后增减 |

两个都在共享区，都只是提示。被谎报时：
- ibnd：只影响不可信读者自己。
- obnd：oob 读者多做或少做一次短读。短读的结果仍然在 `[1, len]` 之间，是合法的返回值。

原来的两个判断本来就和真实状态之间有竞争窗口，所以语义不变。

### 7.4 通知中继：可信 → 不可信

公共设施，见 19 §5：

```c
/* 可信侧，持本端的锁调用 */
void evl_inband_notify_post(struct evl_inband_notify *n)
{
	if (!test_and_set_bit(EVL_NOTIFY_PENDING, &n->flags)) {
		llist_add(&n->node, this_cpu_ptr(&evl_notify_list));  /* 可信内存 */
		irq_post_inband(evl_notify_sirq);                     /* Dovetail 合成中断 */
	}
}

/* 不可信侧：合成中断处理函数 */
static irqreturn_t evl_notify_handler(int irq, void *dev_id)
{
	struct evl_notify_ev ev;

	while (evl_gate_notify_take(&ev))       /* 门：从本 CPU 的链表取一项，清 PENDING */
		ev.fn(ev.cookie, ev.tag);           /* fn 由不可信侧注册，可信侧只存 cookie 和 tag */
	return IRQ_HANDLED;
}
```

xbuf 注册的 `fn` 是 `xbuf_inband_notify(cookie, tag)`：

```c
wake_up(tag == XBUF_IBND ? &ib->rd_wait : &ib->wr_wait);
```

- `n`、链表、PENDING 位都在可信内存里。可信侧不调用任何 Linux 函数。`irq_post_inband` 是 Dovetail 的接口，属于信任基。
- `fn` 是不可信的函数指针，所以只能由不可信的处理函数调用。门 `take` 只交出 `cookie` 和 `tag`，函数指针由不可信侧根据注册时的类型自己查表。可信侧完全不碰函数指针（C3(c)）。
- 唤醒时机和原来相同：irq_work 和合成中断都在下一次回到 inband 时处理。

---

## 8. 门

| 门 | 调用者 | 作用 | 原来的代码 |
|---|---|---|---|
| `xbuf_gate_kick(h, dir, sigpoll_hint)` | inband 读者 / 写者提交以后 | 叫醒 oob 一侧，`evl_schedule` | `inbound_signal_output`、`outbound_signal_input` |
| `evl_gate_notify_take(ev)` | 合成中断处理函数（公共） | 取走一项通知 | `irq_work_run` → `resume_inband_*` |
| `xbuf_gate_release(h)` | `xbuf_release` | `EVL_T_RMID` 叫醒 oob 等待者 | `:553-554` |

创建和销毁随 EVL 元素一起经 factory 的门进入（§9）。

和第一版相比，`reserve_*`、`commit_*`、`readable`、`writable`、`poll`、`rings` 共 8 个门都没有了。原因是 inband 一侧不再需要访问可信状态：它用到的只有共享区，和它自己那一端。

| 操作 | 门调用次数 |
|---|---|
| inband 读或写一次 | 1（门铃）。原来要 2 |
| inband 等待 | 0。等待条件只读共享区 |
| inband poll | 0 |
| 一次通知 | 每一项 1 次 |

---

## 9. 生命周期

| 阶段 | 现在 | 拆分后 |
|---|---|---|
| 创建 `xbuf_factory_build`（`:627-715`） | `copy_from_user` 读 `attrs`（`:636`），检查（`:644-647`）；`kzalloc` 分配 `evl_xbuf` 和两块 `bufmem` | 可信：参数检查不变；`evl_xbuf` 从可信分配器分配；`bufmem` × 2 和 `xbuf_shared` 从共享区分配；两端的 `bufmem`、`bufsz` 各写一份；初始化 EVL 等待对象和两个 `evl_inband_notify`。不可信：Linux 侧分配 `xbuf_inband`，`init_waitqueue_head` 两次，记下句柄和共享区的地址 |
| `release`（`:549-557`） | `evl_flush_wait`、`evl_flush_flag`（`EVL_T_RMID`） | 经门 `xbuf_gate_release`。inband 等待者持有 fd，此时不可能存在 |
| 销毁 `xbuf_factory_dispose`（`:717-731`） | `evl_destroy_wait`、`evl_destroy_flag`、`kfree(bufmem)`、`kfree_rcu` | 可信侧先把两个 `evl_inband_notify` 从链表里撤掉，或者等它们被取走，再销毁 EVL 对象；共享区归还给共享区分配器（C1）；最后由 Linux 侧释放 `xbuf_inband`。顺序反过来，处理函数就可能拿到已经释放的 cookie |

---

## 10. 依赖 `evl_element` 拆分的部分

### 10.1 由 `filp` 找到 xbuf（Q6）

| 入口 | 需要的东西 | 来源 |
|---|---|---|
| inband 读写路径 | `xbuf_inband *` | 可以继续从 `filp->private_data` 拿。它本来就是 Linux 侧的对象，Linux 骗自己没有意义 |
| oob 读写路径 | 可信的 `evl_xbuf *` | 由可信代码按 fd 在 EVL 自己的表里查，不能信 `filp->private_data`（doc 17 §5：oob 系统调用经 Linux 的 fd 表找文件） |
| 门 | 可信的 `evl_xbuf *` | 句柄 → 可信表。第一阶段句柄可以就是指针，校验只报告 |
| `evl_get_xbuf`（`:576-587`） | 判断 fd 是不是 xbuf | 现在比较 `f_op`（`:580`），以后查 EVL 的表，看工厂是不是 `evl_xbuf_factory` |

### 10.2 `O_NONBLOCK`（Q7）

| 入口 | 处理 |
|---|---|
| inband | 照常读 `filp->f_flags` |
| oob | 只决定"阻塞还是返回 `-EAGAIN`"，影响的是调用者自己。先保持现状，以后随 fd 查找一起改成可信侧保存的标志（impl-shared-state/02 的镜像做法） |

---

## 11. 和第一版的差别

| 方面 | 第一版 | 第二版 |
|---|---|---|
| 目标 | 可信状态（包括下标）不被不可信方改坏，通道正确 | 只保证可信内存、实时性、控制流（19 §0） |
| 环的状态 | 整个 `xbuf_ring` 可信，inband 经门预留和提交 | 按端拆开，各域保存自己那一端；`fillsz` 共享，可信端约束后使用 |
| 锁 | 只有可信代码拿，在门内取放 | 每端拿本域的锁，跨域不共用 |
| 门 | 10 个 | 3 个（其中一个是公共的） |
| 不变式 | I1–I5 由门维护 | 不需要。地址安全只靠 §4 的一条论证 |
| 不可信方不提交 | 可信的 `pending` 卡住（第一版 P2） | 不可能，`pending` 是私有的 |
| inband 一侧的 bug | 可能破坏可信状态，所以门要检查 | 只影响通道 |
| 改动 | 中 | 中：分端，`fillsz` 改原子，删回调，加门铃、中继 |

---

## 12. 待确认的问题

| # | 问题 | 建议 |
|---|---|---|
| P1 | 阻塞写 `len > bufsz` 时会一直等下去：`do_xbuf_write` 没有读端那样的 `-EINVAL`（`:229-240`） | 第一阶段保持原样；单独确认以后，在两个写路径里都返回 `-EINVAL`。不影响地址安全，因为拿不到空间就不会拷贝 |
| P2 | 跨 CPU 的唤醒风暴 | 见 19 U1 |
| P3 | `fillsz` 从 `size_t` 改成 `atomic_t` | `bufsz ≤ 2^30`（`:643-647`），放得下。`rings_show` 的格式 `%zu` 要改 |
| P4 | 内存序 | 写端先拷贝数据，再 `atomic_add` 发布；读端先读 `fillsz` 再拷贝数据。在弱内存序的体系结构上要用 `atomic_add_return_release` / `atomic_read_acquire`。这只影响正确性，不影响安全 |
| P5 | `evl_read_xbuf` 在树内没有用户 | 保留，走可信读写路径 |

---

## 13. 不可信方能做什么

| 能 | 不能 |
|---|---|
| 写坏、丢掉、编造两个 `bufmem` 里的数据 | 让可信代码访问 `bufmem` 以外的内存（§4） |
| 改两个 `fillsz`：让 oob 读者读到垃圾，让 oob 写者覆盖未读的数据，或者一直等待 | 碰可信端的 `off`、`rsvd`、`pending` |
| 谎报 `ib_writers`，让 oob 读者多做或少做短读 | 持有可信端的锁（§3：跨域不共用锁） |
| 频繁敲门铃，叫醒 oob 等待者（19 U1） | 让门铃做唤醒以外的事 |
| 不处理通知，让 inband 等待者睡死 | 让可信代码调用不可信函数（§7.4） |
| | 影响别的 xbuf 或别的 EVL 对象（句柄校验） |

---

## 14. 实施步骤

| 步骤 | 内容 | 改变行为？ | 验证 |
|---|---|---|---|
| 1 | 在 `xbuf.c` 内部分端：`xbuf_end` × 4，`fillsz` 改成原子计数，删掉 6 个回调，写 4 条读写路径。通知仍然用 `irq_work`；短读的提示仍然用原来的判断；inband 路径仍然直接调用 EVL 的唤醒函数 | 否 | libevl `basic-xbuf`、`poll-xbuf`；latmus 测延迟 |
| 2 | 加 `oob_writers` / `ib_writers`，代替 `evl_wait_flag_head` / `wq_has_sleeper`；inband 路径的唤醒改成调用 `xbuf_gate_kick` | 否 | 同上 |
| 3 | 通知中继（公共设施）代替 `irq_work`；两个 `wait_queue_head` 搬进 `xbuf_inband` | 否 | 同上，并在 oob 写者高频写入时检查 inband 读者不丢唤醒 |
| 4 | 共享区分配器；inband 路径挪到 `xbuf_inband.c`，由编译器保证它只引用门 | 否 | `nm xbuf_inband.o` |
| 5 | 单独确认并修 P1 | 是，单独提交 | 构造 `len > bufsz` 的阻塞写 |

步骤 1 完成后，跨域就不再共用锁（Q1、Q2）。步骤 2 完成后，oob 不再读 Linux 等待队列（Q4）。步骤 3 完成后，oob 不再调用 Linux 函数，可信对象里也不再嵌 Linux 对象（Q3、Q5）。

---

## 15. 函数对照表

| 原函数 | 行 | 去向 | 域 |
|---|---|---|---|
| `write_to_user` / `write_to_kernel` / `read_from_user` / `read_from_kernel` | 70-99 | 留给可信读写路径；inband 路径用 `copy_*_user` | 可信 / 不可信各一份 |
| `do_xbuf_read` | 101-203 | `xbuf_obnd_read`（可信）和 `xbuf_read` 的读循环（不可信） | 两份 |
| `do_xbuf_write` | 205-292 | `xbuf_ibnd_write`（可信）和 `xbuf_write` 的写循环（不可信） | 两份 |
| `inbound_lock` / `unlock`、`outbound_lock` / `unlock` | 294-309、434-449 | 删除，每端直接取自己的锁 | — |
| `inbound_wait_input` | 311-334 | 进 `xbuf_read`，短读改读 `oob_writers` | 不可信 |
| `resume_inband_reader` / `resume_inband_writer` | 336-341、481-486 | 由 `xbuf_inband_notify` 代替 | 不可信 |
| `inbound_signal_input` / `outbound_signal_output` | 344-349、488-493 | `evl_inband_notify_post` | 可信 |
| `inbound_wait_output` | 351-356 | 进 `xbuf_ibnd_write` | 可信 |
| `inbound_signal_output` / `outbound_signal_input` | 358-366、463-471 | 进 `xbuf_gate_kick` | 可信 |
| `outbound_wait_input` | 451-460 | 进 `xbuf_obnd_read`，短读改读 `ib_writers` | 可信 |
| `outbound_wait_output` | 473-479 | 进 `xbuf_write` | 不可信 |
| `xbuf_read` / `xbuf_write` / `xbuf_poll` | 368-392、400-426 | inband 读写路径 | 不可信 |
| `xbuf_ioctl` / `xbuf_oob_ioctl` | 394-398、428-432 | 不变（返回 `-ENOTTY`） | 不可信 / 可信 |
| `xbuf_oob_read` / `xbuf_oob_write` / `xbuf_oob_poll` | 495-547 | 可信读写路径；poll 用 `fill_of` | 可信 |
| `xbuf_release` | 549-557 | 经门 `xbuf_gate_release` | 可信（入口在 Linux） |
| `evl_get_xbuf` / `evl_put_xbuf` | 576-593 | 依赖 §10.1 | 可信 |
| `evl_read_xbuf` / `evl_write_xbuf` | 595-624 | 可信读写路径 | 可信 |
| `xbuf_factory_build` / `dispose` | 627-731 | §9 | 可信 + 不可信 |
| `rings_show` | 733-752 | 读共享区 | 不可信 |

---

## 16. 实施记录（2026-11-10）

改动在 `linux-evl` 工作区，**未提交**。补丁副本：`patches/2026-11-10-xbuf-split/xbuf-split.diff`。

| 文件 | 内容 |
|---|---|
| `include/evl/inband_notify.h`、`kernel/evl/inband_notify.c`（新） | 通知中继（§7.4）。per-CPU `llist` + 合成中断 `"EVL in-band notify"`；门 `evl_gate_notify_take`；处理函数按 `type` 用 `switch` 分派，可信侧不存函数指针 |
| `kernel/evl/init.c` | `init_core` 里在 `evl_init_memory` 之后调用 `evl_inband_notify_init`，失败路径对称清理 |
| `include/evl/xbuf.h` | 声明 `evl_xbuf_inband_notify(cookie, tag)` |
| `kernel/evl/xbuf.c` | §3 的结构；`fill_of` / `readable` / `writable`；可信路径 `xbuf_obnd_read`、`xbuf_ibnd_write`；不可信路径 `xbuf_read`、`xbuf_write`、`xbuf_poll`；门铃 `xbuf_gate_kick`；6 个回调、两个 `irq_work`、`do_xbuf_read/write` 删除 |

**和设计的差别**（都是为了先把步骤 1–3 做完，剩下的留给步骤 4）：

| # | 差别 | 原因、影响 |
|---|---|---|
| 1 | `struct xbuf_inband` 仍然**嵌在** `evl_xbuf` 里，不单独分配 | 步骤 4 的事。现在可信对象里仍然有两个 `wait_queue_head`，但可信代码不再碰它们 |
| 2 | 共享区（`xbuf_shared`、两块 `bufmem`）用 `kzalloc` 分配 | 共享区分配器还没有（19 §5） |
| 3 | 句柄就是 `evl_xbuf *`，门不校验 | §10.1，依赖 `evl_element` 拆分 |
| 4 | `xbuf_release` 不变，没有改成门 | 它只调用 EVL 函数，等 `evl_element` 拆分时一起改 |
| 5 | 销毁时没有显式撤回待处理的通知，和原来的 `irq_work` 一样依赖 `kfree_rcu` 的宽限期 | 原代码也没有 `irq_work_sync`，属于原有的隐患。§9 的要求仍待实现 |
| 6 | inband 路径用 `copy_to_user` / `copy_from_user`，原来是 `raw_copy_*` | 多一次 `access_ok`，行为不变 |
| 7 | 两个 poll 不再取锁，直接读原子计数 | 只读一个数，取锁没有意义 |
| 8 | `rings_show` 的格式从 `%zu` 改成 `%u`，读的是 inband 一侧的视图 | 数值不变 |
| 9 | 内存序：生产者 `smp_mb__before_atomic()` + `atomic_add`，消费者 `atomic_read_acquire` | P4 |

**验证**：

- `make W=1 kernel/evl/{xbuf,inband_notify,init}.o` 没有警告。checkpatch 没有错误，警告只有 SPDX 位置（和 EVL 其他文件相同）以及原有的 `kfree(NULL)` 检查。
- 当前 HEAD（接入 LKL）的 vmlinux 链接失败：`ld: error: unplaced orphan section '.data.lkl_init'` 等 4 个，都来自 `lkl_blob.o`，和本改动无关（log 10-27 的待办"编译 bzImage"一直没完成）。
- 所以在 HEAD~1（`5f5548b11fbd`，接入 LKL 之前）上打同一个补丁，用 `linux-evl-qemu` 的配置构建，在 QEMU/KVM（4 核）里跑 libevl 全部 77 个测试，并用不打补丁的 HEAD~1 作为基线：

| | 通过 | 失败 |
|---|---|---|
| 基线 | 67 | 10 |
| 打补丁 | 67 | 10，和基线完全相同 |

  失败的是 `fault`、`ring-spray`（rc=127，initramfs 缺依赖）、`fpu-stress`、`hectic`、`observable-race`、`thread-demote`（rc=134）、`ptrace-sync`（rc=137）、`sched-quota-accuracy`、`sched-tp-accuracy`、`sched-tp-overrun`（rc=42，配置里没开对应的调度类）。都和 xbuf 无关，基线上同样失败。
- `basic-xbuf`、`poll-xbuf`、4 个 `proxy-*`、`poll-*` 全部通过。跑完以后，`/proc/interrupts` 里 `EVL in-band notify` 计数为 32，说明 oob → inband 的唤醒确实走的是新的中继。
- **没有测**：延迟（latmus），以及在不可信方篡改共享区的情况下可信端的表现（需要专门的内核测试来故意改写 `fillsz`）。

