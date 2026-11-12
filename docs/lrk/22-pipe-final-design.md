# 22 管道组件（xbuf、proxy）的最终设计

> 2026-11-11。本文是 xbuf 和 proxy 改造的最终设计和实现记录，取代 18、19 两篇中关于这两个组件的方案（18 是第二版"按端拆分 + 共享 `fillsz`"，19 是规则 C1–C7 和 proxy 对照）。本文写成可以单独阅读：没有读过 01–21 的人（或者别的模型）读完本文，应该能理解要解决的问题、采用的设计、代码在哪里、还剩什么没做。
>
> 配图：`docs/report-2026-11-12/fig6-pipe-marks.svg`（流程、标记、谁写谁读）、`fig7-pipe-cut.svg`（截断方法与结果），由同目录的 `fig6gen.py` 生成。

---

## 0. 背景

### 0.1 项目

- **EVL**（Xenomai 4 的实时核心，`linux-evl/kernel/evl/`）和 Linux 运行在同一个内核里。借助 Dovetail 的中断流水线，CPU 有两个执行阶段（stage）：
  - **oob**（out-of-band）：实时阶段，EVL 的实时线程在这里运行，可以随时抢占 Linux；
  - **inband**：普通 Linux 阶段。
- **研究目标**：把系统划成**可信域**和**不可信域**。可信域保证实时线程运行的正确性和实时性；Linux 的大部分代码视为不可信。最终要做到物理上的内存隔离（路线图 P8），当前阶段先做**逻辑上的划分**：明确哪些代码和数据是可信的，可信代码不依赖不可信代码和不可信数据。
- 最初的思路是用 LKL（Linux Kernel Library）承接 EVL 对 Linux 的调用，后来发现这样不够（见 1.1）。

### 0.2 术语

| 术语 | 含义 |
|---|---|
| **Z** | 必须可信的闭包：实时域执行的代码，加上它依赖的数据，加上写这些数据的代码……递归下去 |
| **z** | 无法可信的闭包：纯非实时线程能触及的代码，加上它们能写的数据，加上依赖这些数据的代码……递归下去 |
| **交集** | Z∩z，必须截断的地方 |
| **门（gate）** | 不可信代码进入可信代码的入口函数。门由可信代码实现，只接受数值参数，参数一律当作输入校验 |
| **只记录、不调用** | 可信代码需要不可信一侧做事时，只在可信内存里留一条记录、发一个中断，不调用不可信代码，也不写不可信数据结构 |
| **输入** | 来自不可信方的值。可信代码只能先校验或截断再用，不能用它计算地址、决定控制流，也不能当作指针解引用 |
| **信任基** | 可信代码运行所依赖、但不在本次改造范围内的底层：Dovetail、体系结构代码（包括 `raw_copy_*_user`）等 |

### 0.3 两个组件是什么

- **xbuf**：一对环形缓冲区，用于实时线程和普通线程之间互相传数据。
  - `ibnd` 环：`oob_write()` → `read()`，oob 一侧生产，inband 一侧消费；
  - `obnd` 环：`write()` → `oob_read()`，inband 一侧生产，oob 一侧消费。
- **proxy**：让实时线程能读写一个普通 Linux 文件，比如串口、管道、日志文件。
  - 输出环：`oob_write()` → 环 → 转发者（inband 的 kworker）→ `kernel_write(target)`；
  - 输入环：`kernel_read(target)` → 转发者 → 环 → `oob_read()`。

两者本质相同：**实时域和非实时域共享一条管道，非实时那一端就是不可信端。**

---

## 1. 要解决的问题

### 1.1 为什么不能"把 Linux 调用换成 LKL 调用"

1. **正确性。** EVL 调用 Linux，往往是为了操作 Linux 里的某个具体对象：
   - xbuf 调 `wake_up()`，唤醒的是睡在 Linux 调度器上的任务；
   - proxy 调 `kernel_write(filp)`，写的是用户进程在 Linux 里打开的文件。

   LKL 有自己的调度器和 VFS，没有这些对象。即使把 Linux 的 `filp` 传给 LKL，也只是把执行的代码换成了 LKL，依赖的数据仍然是 Linux 的。
2. **实时性。** 不可信方影响实时域，不只有"被可信代码调用"这一条路。原 xbuf 里还有三条：
   - inband 和 oob 共用一把硬自旋锁；
   - inband 直接修改 `fillsz`；
   - inband 直接调用 `evl_raise_flag_nosched()` 进入 EVL 的调度器。

   这些都不是"EVL 调 Linux"，换成 LKL 也管不到。

### 1.2 保护目标

保护**线程在实时域运行的正确性和实时性**，具体是三样东西：

- **可信内存**：不可信方不能让可信代码越界读写；
- **实时时间**：不可信方不能让可信代码无界地自旋、循环或持锁；
- **可信控制流**：可信代码不调用不可信代码，不拿不可信方能持有的锁，不解引用不可信方给的指针。

**明确放弃**的东西：
- 管道里内容的正确性：对端本来就不可信；
- 不可信方的活性：它可以不写、不读、不唤醒；
- 唤醒的精确性：可以合并，可以多余。

### 1.3 为什么不必保护"管道"本身

管道的一端就是不可信方，所以管道里的内容本来就只能当作输入。篡改元数据（下标、计数）能造成的后果，无非是丢数据、重复、旧数据、乱序、看到空或看到满；对方直接篡改内容，或者干脆不写，也都能造成。

> 篡改元数据能造成的后果 ⊆ 篡改内容能造成的后果

所以保护元数据并没有缩小攻击者的能力。只有三类后果是篡改内容做不到、篡改元数据才能做到的：**越界、时间、控制流**。这三类才需要截断。

**前提**：管道另一端就是不可信方本身。如果两端都可信、只是途经不可信内存，就需要另外做完整性保护。这种情况不在本文范围内。

---

## 2. 分析方法（可复用到别的组件）

1. **按功能流程列出步骤和必需的标记**，不按现有代码的函数和结构体。现有实现可能把不同的东西揉在一起，例如一把锁同时保护了"同端预留"和"两端计数"。
2. **列出每个标记谁写、谁读。**
3. **把具体的域代入**，判断每个标记是否落在 Z∩z，并分两类：
   - **黄色**：Z 受 z 影响，即 z 写的值、z 持有的锁会进入可信代码；
   - **紫色**：Z 要去通知 z。
4. **对每个交集元素选一种截断方法**（第 5 节）。

---

## 3. 管道的流程与标记

一个方向的环。生产者 P、消费者 C 都可能有多个。下面按原实现描述（见 fig6）。

| 生产者 P | 消费者 C |
|---|---|
| P1 加环锁，查空间：`fillsz + 本侧 rsvd + n ≤ bufsz`？ | C1 加环锁，查数据：`fillsz − 本侧 rsvd ≥ n`？ |
| P2 放不下：放锁，在「有空间」上等 | C2 不够：放锁；如果 P 也在等就短读（避免死锁），否则在「有数据」上等 |
| P3 预留：`off = W; W += n; pending++; rsvd += n` | C3 预留：`off = R; R += n; pending++; rsvd += n` |
| P4 放锁，把数据写到 `buf[off]` | C4 放锁，从 `buf[off]` 读数据 |
| P5 加锁：`pending--`，为 0 时 `fillsz += rsvd` | C5 加锁：`pending--`，为 0 时 `fillsz -= rsvd` |
| P6 设置「有数据」：唤醒 C、通知 poll；放锁 | C6 设置「有空间」：唤醒 P、通知 poll；放锁 |

**标记**：

| 标记 | 谁用 |
|---|---|
| W | 只有 P |
| R | 只有 C |
| `fillsz` | 两端唯一共享的进度变量 |
| P 侧预留（`pending`、`rsvd`） | 只有 P |
| C 侧预留 | 只有 C |
| 环锁 | 两端共用 |
| 「有数据」「有空间」两个通知 | P 设置「有数据」，C 设置「有空间」 |
| 「P 在等」提示 | C2 读 |
| `buf` 内容 | P 写，C 读 |

参数类的标记：
- 非阻塞标志：读 `filp->f_flags`；
- fd 对应哪个对象：经 Linux 的 fd 表查找；
- proxy 独有的请求量 `reqsz`；
- proxy 独有的错误和 EOF。

**为什么要有预留**：同一端有多个线程。每个线程在锁里拿到一段不重叠的空间，然后放锁拷贝；最后一个拷贝完的线程（`pending` 降到 0）一次性发布。这样即使先预留的线程后拷贝完，另一端也看不到还没写完的数据。

---

## 4. 原实现里的交集

| 标记 | xbuf A 向 ibnd（P = Z，C = z） | xbuf B 向 obnd（P = z，C = Z） | proxy 输出（P = Z，C = 转发者 z） | proxy 输入（P = 转发者 z，C = Z） |
|---|---|---|---|---|
| `fillsz` | 两边都写 **黄** | 两边都写 **黄** | Z 加、z 减 **黄** | z 加、Z 减 **黄** |
| 环锁 | 两边都持有 **黄** | 两边都持有 **黄** | 只有 Z（转发者不拿） | 只有 Z |
| 「有数据」 | Z 设置、z 在等 **紫** | z 设置、Z 在等 **黄** | Z 设置、转发者处理 **紫** | z 设置、Z 在等 **黄** |
| 「有空间」 | z 设置、Z 在等 **黄** | Z 设置、z 在等 **紫** | z 设置、Z 在等 **黄** | 无，改用请求量 |
| 「P 在等」 | z 拿 Z 的锁去读 Z 的等待者 **黄** | Z 读 z 的等待队列 **黄** | — | — |
| 请求量 | — | — | — | Z 加、z 减 **黄**；Z 要叫醒转发者 **紫** |
| `buf` 内容 | Z 写、z 读 | z 写、Z 读 **黄** | Z 写、z 读 | z 写、Z 读 **黄** |
| 非阻塞标志、fd | z 能改，Z 读 **黄** | 同左 | 同左 | 同左 |
| 错误/EOF | — | — | 无（写错误直接丢弃） | z 写、Z 返回 **黄** |

W、R、两种预留在 xbuf 里都只有一个域在用，原本就不在交集里。

原实现的 proxy 还有 `proxy_write`/`proxy_read`，让 inband 线程也能读写同一个环。这样两端都变成混合端，W、R、预留、环锁都会落入交集。本设计**去掉了这两个接口**，理由见 6.5。

除了上表，原实现还有几处问题：

- **预留窗口的时间耦合**：inband 写者在 P3 到 P5 之间缺页时（`userfaultfd` 能让它无限期挂起），oob 读者一直看不到这批数据。
- **xbuf 的同一份 `do_xbuf_*()` 两域共用**，靠 ring 里的函数指针分派。
- **oob 上调用 `irq_work_queue()`**：会写 Linux 的每 CPU 链表；而嵌在可信对象里的 `irq_work` 标志位又由 Linux 改写，可信代码还要读它。
- **`proxy_oob_poll` 在 oob 上调用目标文件的 `f_op->oob_poll`**：可信代码调用了不可信的函数指针，而且传错了 `filp`。
- **proxy 输入的转发者只按请求量读，不检查剩余空间**：多个读者的请求量加上预读，可能超过剩余空间，覆盖还没被读走的数据。
- **proxy 输出的转发者遇到短写或出错时**，计数按整批扣减，下标却只前进了已处理的部分，两者对不上。

---

## 5. 截断方法

| 方法 | 用于 | 做法 |
|---|---|---|
| **私有副本** | 两域共写的状态 | 可信方保存自己的一份，只信自己这份；需要时发布一份只读副本给对方 |
| **输入化** | z 对 Z 的影响 | z 只能经门传入数值；可信代码截断以后，再修改自己的状态。门是 z 推送输入，读共享字是 Z 拉取输入，两者是同一种方法 |
| **只记录、不调用** | Z 对 z 的通知（紫色） | 可信方只在可信内存里挂一条记录、发一个合成中断；z 自己取走记录后再执行动作。通知送没送到不保证，但发送方式必须是单向的 |
| **归属单侧** | 原本被两域共用、但实际上只属于一侧的东西 | 例如同端预留、同端锁，拆开后各管各的 |
| **消除** | 不再需要的依赖 | 改变流程，让这个依赖消失 |

---

## 6. 最终设计

### 6.1 每个环拆成三部分

| 部分 | 谁写 | 谁读 | 内容 | 保护方式 |
|---|---|---|---|---|
| **可信端** `struct xbuf_tend` / `struct proxy_tend` | 只有可信代码 | 只有可信代码 | `fill`：可信方认定的已写未读字节数（即 `fill′`）<br>`off`：可信侧自己的下标<br>`rsvd`、`pending`：可信侧的预留<br>`waiters`<br>通知记录 `notify`<br>proxy 还有：`req`（未完成的请求量）、`error`、`eof` | 自己的 EVL 等待队列 `wait` 的锁 `wait.wchan.lock`（硬自旋锁）。oob 调用者也睡在这个等待队列上 |
| **不可信端** `struct xbuf_iend` / `struct proxy_iend` | 只有 inband 代码 | 只有 inband 代码 | `off`、`rsvd`、`inflight`、`pending`<br>Linux 等待队列 `wait`<br>proxy 是转发者：`worker_lock`、`off`、`work`、`wq`、inband poll 用的 `pollq` | inband 自己的锁 |
| **共享部分** `struct xbuf_shared` / `struct proxy_shared` 和 `bufmem` | 见右 | 见右 | 可信端发布的 `fill`、proxy 的 `req`、xbuf 的 `oob_waiting`：Z 写，z 只读<br>xbuf 的 `ib_waiting`：z 写，Z 只当提示用<br>`bufmem` 的内容：Z 只当数据用 | 单独分配（`kzalloc`），和可信端分开 |

可信代码**从不读写**不可信端；`bufsz` 在创建后不再改变。

### 6.2 不变式（可信代码保证，无论 z 做什么都成立）

| 编号 | 不变式 |
|---|---|
| I1 | 可信侧计算地址只用可信端的 `off` 和 `bufsz`：`bufmem + off`，且每次拷贝的长度 `n ≤ bufsz − off` |
| I2 | Z 是生产者时：`fill + rsvd ≤ bufsz`。Z 是消费者时：`rsvd ≤ fill ≤ bufsz` |
| I3 | z 经门报告的数量 n 会先截断，再计入可信状态：<br>生产时 `n ≤ bufsz − fill`<br>消费时 `n ≤ fill`<br>proxy 的请求量满足 `fill + req ≤ bufsz` |
| I4 | 可信侧从共享区读的只有两样：`bufmem` 的内容（只当数据拷贝给调用者）、`ib_waiting`（只决定"短读还是继续等"，取任何值都无害）。可信侧不回读自己发布的 `fill`、`req` |
| I5 | 可信代码持有可信锁期间，不执行不可信代码，不访问用户内存（拷贝时一律先放锁），工作量有界 |
| I6 | 可信代码不调用 Linux 的 `wake_up`、`queue_work`、`irq_work_queue`，也不调用目标文件的 `f_op`。通知一律"只记录" |

z 撒谎能造成的后果，只有数据丢失、重复或内容错误，或者 oob 调用者等不到数据或空间。后者只影响这个调用者自己，它本来就选择了阻塞等待。

### 6.3 流程

**Z 生产者**（xbuf `ibnd` 的 `tend_write`；proxy 的 `proxy_oob_write`）
1. 加可信锁。
2. 判断 `fill + rsvd + n ≤ bufsz`。放不下时：非阻塞返回 `-EAGAIN`；阻塞则在可信等待队列上睡眠（`waiters++`，xbuf 还会发布 `oob_waiting`），醒来后重新判断。
3. 预留：`off`、`pending`、`rsvd`。
4. 放锁，`raw_copy_from_user` 到 `bufmem + off`。拷贝出错时把没拷到的部分清零。
5. 加锁，`pending--`。降到 0 时 `fill += rsvd`，发布，然后**只记录**一条通知（xbuf：唤醒 inband 读者；proxy：叫醒转发者）。
6. 放锁，`evl_schedule()`。

**Z 消费者**（xbuf `obnd` 的 `tend_read`；proxy 的 `proxy_oob_read`）

与生产者对称，区别如下：
- 判断条件是 `fill − rsvd ≥ n`；
- 数据不够时，如果 `ib_waiting` 提示有 inband 写者在等空间，就短读，否则等；
- proxy 在第一次等待之前先发请求：`req = min(req + n, bufsz − fill)`，发布并叫醒转发者；被唤醒后先检查 `eof` 和 `error`；
- `pending` 降到 0 时 `fill -= rsvd`；xbuf 还会只记录一条通知，唤醒等空间的 inband 写者。

**z 生产者**（xbuf `obnd` 的 `xbuf_write`）
1. 加 z 锁。空间按 `bufsz − (发布的 fill + rsvd + inflight)` 计算。放不下时：非阻塞返回 `-EAGAIN`；阻塞则 `ib_waiting++`，在 z 自己的 Linux 等待队列上 `wait_event_interruptible`。
2. 预留：`off`、`pending++`、`rsvd += n`。放锁。
3. `copy_from_user` 到 `bufmem + off`。
4. `ib_complete()`：
   - 加 z 锁，`pending--`；降到 0 时把 `rsvd` 整体移到 `inflight`；
   - 放锁，调用门 `xbuf_gate_commit(n)`；
   - 再加锁，`inflight -= n`。

   `inflight` 的作用：在"已经交给门、但还没从本地计数里扣掉"的这段时间里，让别的 z 线程算出的可用量偏保守，避免重复报告，也避免读到未发布的数据。

**z 消费者**（xbuf `ibnd` 的 `xbuf_read`）：与 z 生产者对称。

**门**

| 门 | 做什么 |
|---|---|
| `xbuf_gate_commit(ring, n)` | 加可信锁；按 I3 截断 n；更新 `fill` 并发布；有等待者就 `evl_flush_wait_locked`；放锁；通知 oob 的 poll；`evl_schedule()` |
| `proxy_gate_consumed(n)` | 输出环，转发者报告"写出了 n 字节"。同上，`fill` 为 0 时通知 POLLOUT（proxy 的语义："可写"表示所有数据都已发出） |
| `proxy_gate_produced(n)` | 输入环，转发者报告"读进了 n 字节"。`fill += n`，`req -= min(n, req)` |
| `proxy_gate_status(status)` | 输入环，转发者报告 EOF（`status = 0`）或错误。不是合法负 errno 的值一律当作 `-EIO`；`req` 清零，唤醒读者 |
| `evl_gate_notify_take()` / `evl_gate_notify_done()` | 通知中继：inband 中断处理函数取走一条记录 |

**Z → z 通知**（`kernel/evl/inband_notify.c`）
- `evl_inband_notify_post(n)`：
  - 已经挂起（`PENDING`）就直接返回 false，表示合并；
  - 否则关硬中断，把记录挂到本 CPU 的可信链表上，再 `irq_post_inband` 发一个合成中断。
- inband 中断处理函数逐条取记录。取的时候先置 `BUSY` 再清 `PENDING`，然后按类型执行：
  - `EVL_NOTIFY_WAKE_UP`：对 cookie 调用 `wake_up`；
  - `EVL_NOTIFY_PROXY`：调用 `evl_proxy_inband_notify`，也就是 `queue_work`。（doc 24 的中继第二版改成通用的 `EVL_NOTIFY_CALL`，proxy 用 `relay_kick`。）

  执行完再清 `BUSY`。先清 `PENDING` 再执行，可以保证投递过程中新来的 post 会重新排队，不会丢。
- `evl_inband_notify_drain(n)`：销毁对象前等待 `PENDING` 和 `BUSY` 都清掉，之后才释放 cookie 指向的 inband 对象。
- cookie 由可信方保存，但可信方从不解引用它。

### 6.4 xbuf 的要点

- 两个环的结构相同，`oob_produces` 区分方向，门据此决定截断的方向。
- inband 两个方向都睡在各自 `iend->wait` 上，inband 的 poll 也注册在这里。通知记录的 cookie 就是这个等待队列。
- `xbuf_oob_poll` 只看可信端的 `fill`；`xbuf_poll` 只看发布出来的 `fill`。
- 内核内部接口 `evl_read_xbuf` / `evl_write_xbuf` 走可信端，现在**必须在 oob 上调用**，inband 调用返回 `-EPERM`。inband 内核代码应该走文件接口。
- inband 写入超过 `bufsz` 现在返回 `-EINVAL`。原实现会永远等下去。

### 6.5 proxy 的要点

- **去掉了 inband 的 `read()`/`write()`，返回 `-EOPNOTSUPP`。** 理由有三个：
  - 普通线程可以直接读写目标文件；
  - EVL 线程可以用 `oob_read`/`oob_write`，在 inband 时调用会被自动切到 oob；
  - 去掉以后每个环只有一个可信端和一个不可信端（转发者），W、R、预留、环锁都不再跨域。

  代价是：同一个输出流里，oob 和 inband 两边输出的先后顺序不再保证。
- **libevl 相应修改**（`libevl/lib/proxy.c`）：
  - `evl_write_proxy`/`evl_read_proxy` 改为直接调用 `oob_write`/`oob_read`。原来用的 `__evl_transparent_call` 会把成功时返回的字节数变成 0 或旧的 errno，这次一并修好；
  - `evl_printf`/`evl_eprintf` 在调用线程没有 attach 时，直接写 stdout/stderr。
- **转发者**：
  - 不拿可信锁，只用自己的 `worker_lock` 和 `f_pos_lock`；
  - 输出转发者读发布的 `fill`，写出后报告；即使出错，也按整批前进自己的下标，保持和报告的数量一致；
  - 输入转发者读发布的 `req`，每次 `kernel_read` 之后按实际读到的字节数报告，下标也按实际字节数前进。
- **请求量 `req` 改成可信端私有**，由可信端截断到剩余空间以内，原实现可能写越界的问题随之消失。`proxy_oob_poll` 在输入环为空、且没有未完成请求时，会发出一次预读请求。
- **删除了 `proxy_oob_poll` 对目标文件 `f_op->oob_poll` 的转发。**
- **inband 的 `.poll` 保留，而且整个属于 z**：它只读可信端发布的 `fill`，并转发给目标文件的 `f_op->poll`（inband 调 inband）。环的就绪状态只会因转发者的动作而变好：输出环被转发者写出，输入环被转发者读入。所以由转发者在报告之后自己 `wake_up(&ib->pollq)`，可信端不需要通知 inband 的 poll。libevl 的 `proxy-poll` 测试依赖这个接口：它在 inband 用 epoll 等 proxy fd 变成可读。
- **生命周期**：
  - 可信端叫醒转发者时，先给 element 加一个引用，再挂记录；记录本来已经挂着时，立即释放这个引用；
  - inband 中断处理函数调用 `queue_work`：成功就把引用交给工作项，工作项已经在队列里就释放引用；
  - 转发者运行结束时释放引用。

  所以只要还有记录没投递、或者转发者在排队或运行，proxy 就不会被销毁。`dispose` 总是在 flusher 内核线程或关闭路径里运行，不会在转发者的 kworker 里运行（见 `__evl_put_element`），所以可以安全地调用 `cancel_work_sync` 和 `destroy_workqueue`。

### 6.6 与之前方案的区别

| 方案 | 做法 | 问题 |
|---|---|---|
| 18 第一版 | 保护管道：下标归可信域，z 经门预留和提交 | 可信端要记录"z 预留了还没提交"的状态，等 z 提交完才能发布，等于被 z 拖住 |
| 18 第二版（已废弃，实现在 `patches/2026-11-10-xbuf-split/`） | 按端拆分，两端共享一个原子 `fillsz` | z 不必越界，只要把 `fillsz` 写成 `bufsz` 或 0，就能让可信侧永远见满或永远见空 |
| **本设计** | 可信端自己保存 `fill′`，z 只经门报告数量；同端预留归单侧，z 侧由最后一个完成的线程统一报告 | — |

**关于"可信端从不等待"**：讨论中设想过让可信写者满了不等、改为告警（fig7 里画成了"消除"）。实现时保留了**阻塞语义**，理由有三个：
- libevl 的 `proxy-eventfd` 等程序依赖反压：缓冲区满了，`oob_write` 就阻塞；
- 可信调用者睡在**可信等待队列**上，只能由可信代码（门）唤醒。z 不响应时，被拖住的只是这个调用者自己，不影响其他实时任务，也不影响可信内存和控制流；
- 非阻塞调用者照样得到 `-EAGAIN`。

超时告警留作后续工作（第 8 节）。

---

## 7. 行为变化（相对原实现）

| 项 | 原来 | 现在 |
|---|---|---|
| proxy 的 inband `read()`/`write()` | 支持 | 返回 `-EOPNOTSUPP` |
| proxy 的 inband `poll` | 支持，并转发目标文件的 poll；由 inband 读写者和转发者唤醒 | 保留，属于 z：只读发布值并转发目标文件的 poll，由转发者唤醒 |
| `proxy_oob_poll` | 合并目标文件的 `oob_poll` | 不合并 |
| proxy 输入的转发者 | 按请求量读，不看剩余空间 | 请求量由可信端截断到剩余空间以内 |
| libevl `evl_write_proxy`/`evl_read_proxy` | inband 时走 `write`/`read`，返回值有误 | 总是 `oob_write`/`oob_read`，返回字节数 |
| libevl `evl_printf`（未 attach 的线程） | 写 proxy | 直接写 stdout/stderr |
| xbuf 的 inband `write()`，长度大于 `bufsz` | 永远等待 | `-EINVAL` |
| `evl_read_xbuf`/`evl_write_xbuf` | inband 加 `O_NONBLOCK` 也可以调用 | 只能在 oob 上调用 |

---

## 8. 验证

- 内核：`make O=../build/linux-evl-qemu -j64 bzImage`。EVL 源码没有新增警告。
- libevl 测试套件（77 个），外加专门的压力测试 `tools/pipe-stress/pipe-stress.c`，覆盖四种情形：
  1. 4 个 inband 写者 → 1 个 oob 读者（xbuf，64 字节的环，各 3000 条记录，检查逐写者的顺序）；
  2. 2 个 oob 写者 → 2 个 inband 读者（检查不丢不重）；
  3. proxy 输入：管道 → 转发者 → `oob_read`，2 万字节逐字节校验；
  4. proxy 输出：2 个 oob 写者 → 管道 → inband 读者，检查逐写者的顺序。
- 结果见第 10 节。

---

## 9. 尚未解决、接下来的任务

| # | 事项 | 说明 | 归属 |
|---|---|---|---|
| N1 | fd → 对象、`f_flags` | oob 调用仍然经 Linux 的 fd 表、`filp->private_data` 找到 xbuf/proxy，非阻塞标志读的仍然是 `filp->f_flags`。按 doc 20，要换成可信句柄表，并在句柄里记录打开方式。门目前接受的 `ring` 指针也来自 z，句柄表做好后改成句柄 | 路线图 P4a、P5 |
| N2 | 生命周期 | `release` 由 Linux 的 VFS 触发，然后 z 调用可信的 `evl_flush_wait(RMID)`；销毁由 factory 的 kthread、RCU 驱动 | factory，规则 C7 |
| N3 | 门的调用频率 | z 可以不停地调用门。每次调用工作量有界；唤醒只在可信状态变化时发生；有数据的次数受环容量和消费速度限制。但门里的 `evl_schedule()`、`evl_signal_poll_events()` 被频繁调用时的开销还没有量化 | 门铃风暴问题 U1 |
| N4 | 超时与告警 | 可信侧等待没有超时、没有积压告警，需要可观测性设施（计数器、trace） | 后续 |
| N5 | 用户内存访问 | 可信路径用 `raw_copy_*_user` 访问调用者自己的缓冲区，归入信任基；缺页时会进入 Linux 的缺页处理（问题 U2）。需要一条规则 C8 来约束 | 路线图 P1、P8 |
| N6 | 物理隔离 | `shared` 和 `bufmem` 要从专用的共享区分配器分配（规则 C1），可信端和不可信端要放进不同的保护域；门要变成真正的跨域入口 | 路线图 P8 |
| N7 | `proxy_mmap` | 仍然由 inband 转发给目标文件的 `f_op->mmap`。它在 inband 执行，不在可信路径上，但属于 z 对 Z 对象的操作，需要在对象模型里归类 | P4a |
| N8 | 文档同步 | 18、19 的方案以本文为准；fig5（PPT 第 4 页）里"`off = ib_wroff`"的画法已经过时 | — |

---

## 10. 实现记录

改动尚未提交。

| 文件 | 改动 |
|---|---|
| `linux-evl/include/evl/inband_notify.h`、`kernel/evl/inband_notify.c` | 新增：通知中继（PENDING/BUSY、drain、按类型分派） |
| `linux-evl/kernel/evl/init.c`、`Makefile` | 初始化和清理通知中继 |
| `linux-evl/kernel/evl/xbuf.c` | 重写，按 6.1–6.4 |
| `linux-evl/kernel/evl/proxy.c` | 重写，按 6.1–6.3、6.5 |
| `libevl/lib/proxy.c` | `evl_write_proxy`、`evl_read_proxy`、`evl_vprint_proxy` |
| `tools/pipe-stress/pipe-stress.c` | 新增压力测试 |

旧的第二版实现备份在会话临时目录的 `backup-v2/`，补丁在 `patches/2026-11-10-xbuf-split/`。

测试结果（2026-11-11，QEMU，KVM，4 核，`nokaslr`）：

- libevl 77 个测试：67 个通过，10 个失败。失败的 10 个和改动前的基线完全相同：`fault` 和 `ring-spray` 缺共享库；`fpu-stress`、`hectic`、`observable-race`、`thread-demote`、`ptrace-sync` 是原有问题；三个 `sched-*` 失败是因为配置里没开对应的调度类。
- 和管道相关的测试全部通过：`basic-xbuf`、`poll-xbuf`、`proxy-echo`、`proxy-eventfd`、`proxy-pipe`、`proxy-poll`、`mapfd`、`element-visibility`、`file-dup`、`poll-close`。
- `pipe-stress` 的 4 项全部通过。
- dmesg 里没有 WARNING、BUG 或 Oops。
- 第一轮跑出两个问题：
  - `proxy-poll` 失败：第一版删掉了 proxy 的 inband `.poll`，后来按 6.5 恢复为只属于 z 的实现；
  - 压力测试第 3 项失败：阻塞的 `oob_read` 在 EOF 前读不够时会返回 0，这是原有语义，问题出在测试本身，已改为按 4 字节读。
