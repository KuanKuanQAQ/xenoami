# observable.c：发布 / 订阅通知

> 源码：`kernel/evl/observable.c`（1169 行）、`include/evl/observable.h`、`include/uapi/evl/observable-abi.h`。分析结果：`tools/trust-closure/out/subsys/observable.txt`。

## 1. 功能

observable 是观察者模式的通知通道：

- **订阅**：任何线程，EVL 的和普通 Linux 的都可以，都能订阅一个 observable，同时指定自己最多积压多少条通知（backlog）。
- **发布**：发布者发出一条通知（notice）= 一个标签 `tag` 加一个值 `event`。发布者可以是 oob 上的 EVL 线程、inband 上的 Linux 线程，也可以是内核代码。
- **接收**：订阅者 `read`/`oob_read` 拿到通知，每条带上序号、发布者的 pid 和时间戳。

| 选项 | 含义 |
|---|---|
| 广播（默认） | 每条通知发给所有订阅者 |
| 单播 `EVL_CLONE_UNICAST` | 每条通知只发给一个订阅者，轮流发（round-robin），用来做工作线程池 |
| `EVL_NOTIFY_ONCHANGE`（按订阅者设置） | 和上一条通知完全相同（`tag` 和值都一样）的就丢掉，只在变化时通知 |

**EVL 线程本身就是 observable**：`evl_notify_thread`（`thread.c:1464`）在线程设置了 `EVL_T_HMOBS` 时，通过线程自己的 observable 发出健康监测事件（比如意外切换到 inband）。这个调用可能发生在 oob 上。所以 observable 工厂的设备数是"observable 数 + 线程数"。

## 2. 原理

### 2.1 三层对象

```
task_struct ──(Dovetail 的 per-task oob 状态)──▶ evl_subscriber    每个线程一个，懒创建
                                                   └─ subscriptions：红黑树，按 observable 的句柄索引
                                                        └─ evl_observer     每个"线程 × observable"一个
                                                             ├─ backlog[]：预先分配好的通知记录
                                                             ├─ free_list / pending_list
                                                             └─ next：挂在 evl_observable.observers 上
evl_observable ── observers 链表 ── 所有订阅它的 evl_observer
```

- **订阅者只记 observable 的句柄（fundle），不保存指针**。这样 observable 可以先于订阅者消失，订阅变成"过期"状态，这是有意的松耦合（`:104-111` 的注释）。
- **通知记录全部预先分配**：发布时只是从 `free_list` 摘一条记录、填好、挂到 `pending_list`，**oob 上不需要分配内存**。backlog 用完了，这个订阅者就收不到新通知（发布者通过返回值知道没有人收到）。

### 2.2 发布（`push_notification`）

1. 先在栈上准备一条模板记录：`tag`、`event`、发布者 pid（只有用户标签才记录，即 `tag >= EVL_NOTICE_USER`）、时间。同一次发布的所有接收者拿到完全相同的内容，包括时间戳；
2. 持 `observable->lock`，分配序号 `serial_counter++`；
3. 单播：取链表头的订阅者，把它挪到链表尾（轮转）；广播：遍历全部订阅者；
4. 对每个订阅者，在 `oob_wait.wchan.lock` 下调用 `notify_one_observer`：处理 ONCHANGE 过滤，摘一条空闲记录拷进去，挂到待读链表。如果空闲链表因此变空，`writable_observers--`（用于 POLLOUT）；
5. **遍历时不一直持锁**：对当前和下一个订阅者各加一个引用（`refs`），放锁去通知，再持锁放引用。如果某个订阅者在这期间被取消订阅，引用归零，就摘下来放到 `flush_list`，随后在 inband 上释放。oob 上不能 `kfree`，所以在 oob 上要先挂 irq_work（`flush_irqwork`）。

发布成功以后，`wake_up_observers`：踢醒所有 oob 等待者，发 POLLIN 事件，再唤醒 inband 读者（在 oob 上就走 irq_work）。

### 2.3 接收（`pull_notification`）

1. 等到 `pending_list` 非空：oob 上用 EVL 等待队列（`pull_from_oob`）；inband 上同时持 Linux 等待队列锁和 EVL 等待队列锁（`pull_from_inband`），用 `schedule()` 睡眠；
2. 摘下记录，转换成 `__evl_notification`，**放锁后**拷给用户；
3. 持锁：拷贝失败的话，把记录放回 `pending_list` 的头部，不丢通知；成功的话放回 `free_list`。如果这个订阅者因此从"满"变成"可写"，`writable_observers++`，并通知写者（POLLOUT，inband 写者在 oob 上要走 irq_work）。

### 2.4 锁

| 锁 | 类型 | 保护 |
|---|---|---|
| `observable->lock` | hard lock | `observers` 链表、`flush_list`、`serial_counter`、订阅者的 `refs` |
| `observable->oob_wait.wchan.lock` | hard lock（EVL 等待队列自带） | 所有订阅者的 `free_list`/`pending_list`、`last_notice`、`flags`、`writable_observers` |
| `evl_subscriber->lock` | hard lock | 订阅者自己的红黑树（插入、删除时） |

嵌套顺序是先 `observable->lock` 再 `oob_wait.wchan.lock`。inband 读者是先 Linux 等待队列锁，再 `oob_wait.wchan.lock`（`:789-791`，参见 stax 的说明）。

## 3. 数据结构

### 3.1 `struct evl_observable`

| 字段 | 含义 |
|---|---|
| `element` | EVL element；`clone_flags` 里的 `EVL_CLONE_UNICAST`、`EVL_CLONE_OBSERVABLE` 决定行为 |
| `observers` | 订阅者链表（`evl_observer.next`），按订阅顺序 |
| `flush_list` | 等待释放的订阅者 |
| `oob_wait` | oob 读者的等待队列；它的锁保护所有订阅者的记录链表 |
| `inband_wait_r` / `inband_wait_w` | Linux 等待队列：inband 读者 / inband poller 等 POLLOUT |
| `poll_head` | oob poll |
| `wake_r_irqwork` / `wake_w_irqwork` / `flush_irqwork` | 三个从 oob 推迟到 inband 的跳板：唤醒读者、唤醒写者、释放订阅者 |
| `lock` | hard lock（§2.4） |
| `serial_counter` | 下一条通知的序号 |
| `writable_observers` | 还有空闲记录的订阅者个数，大于 0 时可写（POLLOUT） |

### 3.2 `struct evl_subscriber`（每个线程一个）

| 字段 | 含义 |
|---|---|
| `subscriptions` | 红黑树，节点是 `evl_observer.rb`，键是 observable 的句柄 |
| `lock` | 保护树的修改 |

它的指针放在 **Dovetail 的 per-task 状态**里（`dovetail_current_state()->subscriber`，`include/evl/thread.h:258`），也就是放在 Linux 的 `task_struct` 里。

### 3.3 `struct evl_observer`（每个订阅一个）

| 字段 | 含义 |
|---|---|
| `backlog_size` | backlog 的字节数 |
| `free_list` / `pending_list` | 空闲记录 / 待读记录 |
| `next` | 挂在 `observable->observers`（或 `flush_list`）上；**为空表示已被 observable 强制解除订阅**（`pull_from_oob` 据此返回 `-EBADF`） |
| `fundle` | 订阅的 observable 的句柄 |
| `rb` | 在订阅者红黑树里的节点 |
| `flags` | `EVL_NOTIFY_ONCHANGE`，以及内部的 `EVL_NOTIFY_INITIAL`（第一条通知不过滤） |
| `refs` | 发布遍历时防止被释放的引用计数，初值 1 |
| `last_notice` | ONCHANGE 过滤用的上一条通知 |
| `backlog[]` | 柔性数组：`backlog_count` 条通知记录 |

### 3.4 `struct evl_notification_record` 和用户态结构

| 结构 | 字段 |
|---|---|
| `evl_notification_record`（内核） | `tag`、`serial`、`issuer`（pid）、`event`、`date`（ktime）、`next` |
| `evl_notice`（用户写入） | `tag`（用户标签必须 ≥ `EVL_NOTICE_USER` = 64）、`event` |
| `__evl_notification`（用户读到） | `tag`、`serial`、`issuer`、`event`、`date`（timespec） |
| `evl_subscription`（订阅请求） | `backlog_count`、`flags` |

## 4. 函数语义

| 函数 | stage | 语义 |
|---|---|---|
| `index_observer` / `find_observer` | 两者 | 在订阅者红黑树里按句柄插入 / 查找。插入用的是 `lkl_evl_rb_insert_color`，也就是**第一阶段迁移到 LKL 的红黑树辅助函数** |
| `add_subscription(observable, backlog_count, flags)` | inband（有断言） | 懒创建订阅者；分配订阅者对象和 backlog（一次 `kzalloc`）；插入红黑树（重复订阅返回 `-EEXIST`）；建空闲链表；挂到 observable 上，`writable_observers++`；发 POLLOUT |
| `decrease_writability` | 持 `oob_wait` 锁 | `writable_observers--` |
| `detach_observer_locked` / `detach_observer` | 两者 | 从 observable 上摘下；后者先放一个引用，引用归零才摘 |
| `get_observer` / `put_observer` | 持 `observable->lock` | 引用计数 |
| `cancel_subscription` | inband（有断言） | 从红黑树删除（LKL 的 `rb_erase`），摘下，引用归零就释放 |
| `evl_drop_subscriptions(sbr)` | inband（线程退出时，`thread.c:1992`） | 清理这个线程的所有订阅；过期的订阅（observable 已不在）只释放内存 |
| `evl_flush_observable` | inband（release 或线程清理 `do_cleanup_current` 时，`thread.c:328`） | 解除所有订阅者（`list_del_init`，变成过期），踢醒 oob 等待者 |
| `observable_release` | inband | `evl_flush_observable` 后释放 element（会等正在进行的 oob 操作结束） |
| `do_flush_work` / `inband_flush_irqwork` | inband | 释放 `flush_list` 上的订阅者 |
| `notify_one_observer` / `notify_single` / `notify_all` | 两者 | §2.2 |
| `is_pool_unicast` | 两者 | 读 `clone_flags` |
| `push_notification` | 两者 | §2.2；有需要释放的订阅者时，inband 上直接释放，oob 上挂 irq_work |
| `wake_up_observers` | 两者 | 唤醒 oob 读者和 inband 读者（oob 上走 irq_work） |
| `evl_send_observable(observable, tag, details)` | 两者（内核 API，线程的健康监测会在 oob 上调用） | 发一条内核通知 |
| `evl_write_observable(observable, u_buf, count)` | 两者 | 用户写：`count` 必须是 `evl_notice` 大小的整数倍；逐条拷贝，标签必须 ≥ 64；有人收到才唤醒 |
| `pull_from_oob` / `pull_from_inband` | oob / inband | §2.3 第 1 步 |
| `pull_notification` | 两者 | §2.3 |
| `evl_read_observable(observable, u_buf, count, wait)` | 两者 | 必须已订阅；`count` 必须是记录大小的整数倍；第一条可以等，之后的不等，能拿多少拿多少 |
| `poll_observable` / `evl_oob_poll_observable` / `evl_poll_observable` | 两者 | 有待读记录 → POLLIN（只对订阅者）；`writable_observers > 0` → POLLOUT |
| `evl_ioctl_observable` | inband | SUBSCRIBE / UNSUBSCRIBE |
| `observable_*` 文件操作 | 各自的 stage | 转调上面的函数，`O_NONBLOCK` 取自 `filp->f_flags` |
| `evl_alloc_observable` | inband | 分配并初始化，登记命名空间（线程创建时也调用它，`thread.c:2570`） |
| `observable_factory_build` / `dispose` | inband | 创建 / 销毁 |

## 5. 可信域划分

按 [README 的规则 1–10](README.md#划分规则)。字段级的原始结果在 `tools/trust-closure/out/subsys/observable.txt`（种子见 `cfg/observable.json`），下面是在它基础上人工推导的类型级结论。

### 5.1 代码：入口和调用链（规则 1、2）

| 入口 | 调用者 | 进入的函数 | 参数 | 结论 |
|---|---|---|---|---|
| oob 系统调用 | EVL 核心分派 | `observable_oob_read/write/poll` | `filp` 来自 `evl_file`；通知内容是用户输入 | 可信 |
| oob 系统调用（线程作为 observable） | `thread.c` 的线程 fops（`:2354-2399`） | `evl_read/write_observable`、`evl_oob_poll_observable` | `thread->observable` 是可信的 `evl_thread` 字段 | 可信；thread.c 的这些 fops 也是可信代码 |
| 健康监测 | `evl_notify_thread` ← 调度器（`sched/core.c`、`sched/tp.c`）、`mutex.c`、`wait.c`、`stax.c` | `evl_send_observable` | 标签和细节按值传入 | 可信；这些调用者本来就是 EVL 核心 |
| inband 系统调用 | VFS（Linux） | `observable_read/write/ioctl/poll` | `filp` 是 Linux 给的 | 可信代码（订阅、改记录链表），入口在 Linux，找对象要核对（README 结论 3） |
| 关闭 | VFS | `observable_release` | 同上 | 可信 |
| inband 唤醒 | Linux 的 irq_work | `inband_wake_r_irqwork`、`inband_wake_w_irqwork` | `work` 来自 Linux | **问题**：`wake_up` 之后 `evl_put_element`，写可信引用计数（同 monitor） |
| 推迟的清理 | Linux 的 irq_work | `inband_flush_irqwork` → `do_flush_work` | `work` 来自 Linux | **问题更大**：遍历 `flush_list`、释放 observer、`evl_put_element`，都是写可信状态 |
| 任务退出 | Dovetail 的 `INBAND_TASK_EXIT` 钩子（`thread.c:1992`，从 Linux 的退出路径调用） | `evl_drop_subscriptions(evl_get_subscriber())` | **订阅者指针从 `task_struct` 里取** | **问题**：可信代码释放一棵可信的树，树根由 Linux 对象里的指针给出 |
| element 释放 | Linux 的 irq_work（`do_put_element_irq`） | `observable_factory_dispose` | `work` 来自 Linux | element 生命周期的共性问题 |

规则 2 的检查：inband 读的等待循环用 `set_current_state`、`schedule`、`signal_pending`，醒来后在可信锁下重查，**符合**。`task_pid_nr(current)` 的结果写进通知记录的 `issuer`，这是 Linux 函数的返回值进入可信对象，但 EVL 从不拿它做判断，只是透传给读者的数据，**可以接受**（不透明数据）。`irq_work_queue` 在 oob 上调用、返回值决定引用计数，**不符合**。

### 5.2 分配单元（规则 4、5、6）

| 分配单元 | 分配点 | 结论 |
|---|---|---|
| `struct evl_observable` | `evl_alloc_observable`：`kzalloc(sizeof(*observable))` | **可信** |
| `struct evl_subscriber` | `add_subscription`：`kzalloc(sizeof(*sbr))`，每个任务一个 | **可信**：oob 读写经它找到自己的订阅 |
| `struct evl_observer` | `add_subscription`：`kzalloc(sizeof(*observer) + backlog_size)`，**变长**，backlog 记录数组嵌在尾部 | **可信**。`evl_notification_record` 嵌在里面，跟随它（规则 6） |
| `evl_notice`、`evl_subscription` | 栈，`raw_copy_from_user` | 用户输入，按值，规则 3 |

嵌入的成员按规则 6 跟随容器：

- `evl_observable`：`element`、`observers`、`flush_list`、`oob_wait`（`evl_wait_queue`）、`poll_head`、`lock`、`serial_counter`、`writable_observers`。**例外是 5.4 的 Linux 成员。**
- `evl_subscriber`：`subscriptions`（红黑树根）、`lock`。
- `evl_observer`：两条记录链表、`next`（挂在 observable 上）、`rb`（挂在订阅者的树上）、`last_notice`、`backlog[]`。

一个 observer 同时挂在两个分配单元上：observable 的链表和订阅者的树。两者都可信，所以不冲突。

### 5.3 写者（规则 4）

| 类型 | 写者 |
|---|---|
| `evl_observable` | 发布路径（`push_notification`、`notify_one_observer`）、订阅和取消（`add_subscription`、`cancel_subscription`）、清理（`do_flush_work`）、工厂；EVL 核心的 wait、poll、factory |
| `evl_subscriber` | `add_subscription`（创建）、`cancel_subscription`、`evl_drop_subscriptions`（释放） |
| `evl_observer` 和记录 | 发布路径（取空闲记录、挂到 pending）、接收路径（`pull_notification` 取走、放回空闲）、订阅和清理 |
| **订阅者指针所在的槽** | `evl_set_subscriber`（observable.c）、`evl_init_thread_state`（Dovetail 在 fork 时初始化）。**这个槽在 `task_struct` 里**，见 5.5 |
| **Linux：等待队列代码、irq_work 核心、RCU、cdev** | `evl_observable` 里嵌入的 Linux 成员，见 5.4。按规则必须消除 |

### 5.4 嵌入的 Linux 成员（规则 8）

| 成员 | 谁写 | 现在怎么用 | 改法 |
|---|---|---|---|
| `inband_wait_r`（`wait_queue_head`） | Linux 等待代码 | inband 读者在上面睡；**oob 发布时用 `waitqueue_active` 偷看** | 单独分配在不可信域；偷看改成 EVL 自己的计数 |
| `inband_wait_w`（`wait_queue_head`） | 同上 | inband 写者（poll POLLOUT）在上面睡；**oob 接收时偷看** | 同上 |
| `wake_r_irqwork`、`wake_w_irqwork`、`flush_irqwork` | Linux irq_work 核心 | oob 上 `irq_work_queue`，返回值决定 `evl_put_element` | 换成 EVL 自己的 inband 中继。`flush_irqwork` 的回调要做大量可信操作，更需要由 EVL 自己的中继来调用 |
| `element` 里的 `rcu`、`cdev`、`irq_work` 等 | RCU、字符设备核心、irq_work | 所有 element 共有 | EVL 核心拆分 `evl_element` |

### 5.5 依赖的 Linux 分配单元（规则 9）

| Linux 对象 | 字段 | 在哪里读 | 截断方式 |
|---|---|---|---|
| `task_struct` 里的 `oob_thread_state` | `subscriber` | `evl_get_subscriber()`：oob 上的 `evl_read_observable`、`evl_oob_poll_observable`，inband 的订阅和退出清理 | 见下 |
| `struct file` | `f_flags`（`O_NONBLOCK`） | 读写路径，含 oob | 镜像（M3） |
| `struct file` | `private_data` | `element_of`，含 oob | 从 `evl_file` 找对象（M4） |

**`subscriber` 指针怎么处理。** 它是"一个可信分配单元的根指针存放在 Linux 分配单元里"的典型。有三个选择：

1. **移到 `evl_thread` 里**。可信，但不行：注释（`:25-30`）说明**非 EVL 线程也要能订阅**，它们没有 `evl_thread`。
2. **EVL 自己维护一张"任务 → 订阅者"的表**，在可信域里，以 pid 或任务指针为键。oob 查表代替读 `task_struct`。代价是 oob 上多一次查找。
3. **当作运行时底座**，写成信任假设，读的时候校验（例如检查指针落在订阅者的分配区域里）。

要注意，`oob_thread_state` 里一共只有三个字段：`thread`、`subscriber`、`preempt_count`。**`thread` 就是 `evl_current()` 的来源**，几乎所有 EVL 代码都经它找到当前线程。所以 `subscriber` 不是 observable 独有的问题，而是"EVL 的 per-task 状态放在 `task_struct` 里"这个共性问题的一个实例，应该和 `evl_current()` 一起决定（选择 3 是现实的起点，doc 13 §11.5）。

### 5.6 小结

| 问题 | 结论 |
|---|---|
| 可信代码 | observable.c 里除两个唤醒回调外的全部函数（`inband_flush_irqwork` 本身也是可信代码，只是入口要改）；调用链上的 thread.c 线程 fops、`evl_notify_thread` 及其调用者 |
| 可信分配单元 | `evl_observable`、`evl_subscriber`、`evl_observer`（含 backlog 记录） |
| 需要改的地方 | 两个等待队列头移出、三个 irq_work 换掉（规则 8）；`subscriber` 指针、`f_flags`、`private_data` 截断（规则 9）；退出钩子、irq_work 回调、inband 入口核对指针（规则 1） |

## 6. 值得注意的地方

1. **第一版工具漏掉了 `subscriber`**：`evl_get_subscriber` 是 EVL 的内联函数，按配置被当成"EVL 核心，固定可信"。按规则 4，它属于"隐式状态"，必须单独追踪。下一步应该把 `include/evl/*.h` 里的内联函数纳入分析范围。
2. observable 的 Linux 入口最多：三个 irq_work 回调加一个退出钩子，其中 `inband_flush_irqwork` 和 `evl_drop_subscriptions` 都在做大量的可信操作。这说明规则 1 的"Linux 入口要有一道门"在这里最迫切。
3. `backlog_count` 只检查了大于 0，没有上限，用户可以请求很大的 `kzalloc`。只发生在 inband 上，只影响内存，不影响实时性。
4. 发布遍历时用"当前和下一个都加引用"的技巧，避免整个遍历都持锁（`:556-565` 的注释）。但遍历期间新加入的订阅者收不到这一条通知，注释认为这是合理的。
5. 读到的记录在锁外拷给用户，拷贝失败就放回 `pending_list` 的头部（`:860-866`），保证不丢通知。代价是顺序可能被打乱，可以靠时间戳重新排序。
