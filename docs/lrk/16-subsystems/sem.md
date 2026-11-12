# sem.c：内核信号量 evl_ksem

> 源码：`kernel/evl/sem.c`（68 行）、`include/evl/sem.h`。分析结果：`tools/trust-closure/out/subsys/sem.txt`。

## 1. 功能

`evl_ksem` 是给**内核代码**用的计数信号量，用户态看不到它。典型用法是驱动里的"生产者 / 消费者"：

```c
DEFINE_EVL_KSEM(sem, 0);
/* 消费者，在 oob 上的 EVL 线程里 */
evl_down(&sem);              /* 计数为 0 就睡 */
/* 生产者，在 oob 中断处理或另一个 EVL 线程里 */
evl_up(&sem);                /* 计数加 1，唤醒一个等待者 */
```

树内唯一的用户是 `drivers/spi/spidev.c`（`CONFIG_SPI_SPIDEV_OOB`）。它把初值为 1 的 ksem 当互斥锁用，保护 oob 传输的使能状态 `oob.enabled` 和传输描述符 `oob.xfer`：

| spidev 函数 | stage | 对 ksem 的操作 |
|---|---|---|
| `init_oob_state`（`spidev_open` 调用） | inband | `evl_init_ksem(&spidev->oob.sem, 1)` |
| `enable_oob_mode` / `disable_oob_mode`（inband ioctl 调用） | inband | `evl_trydown` … `evl_up` |
| `run_oob_transfer`（`spidev_oob_ioctl` 调用） | **oob** | `evl_down` … `evl_up` |
| `release_oob_state`（`spidev_release` 调用） | inband | `evl_destroy_ksem` |

> 第一版写的是"树内没有任何调用者"，**这是错的**：当时只搜了 `kernel/evl` 和 `drivers/evl`。

## 2. 原理

信号量 = 一个计数 `value` + 一个 EVL 等待队列 `wait`。

- `down`：如果 `value > 0` 就减 1 返回；否则把当前线程挂到 `wait` 上睡眠，被唤醒后重新检查。
- `up`：`value` 加 1，叫醒等待队列里排在最前面的线程（按优先级排序，`EVL_WAIT_PRIO`）。
- `broadcast`：`value` 清零，叫醒所有等待者，并给它们打上 `EVL_T_BCAST` 标记。被唤醒的线程看到这个标记后**不会**重新检查条件，直接返回；它们拿到的不是一个单位，而是"有人广播了"这个通知。

所有对 `value` 的读写都在 `wait.wchan.lock`（`hard_spinlock_t`，关硬件中断）下进行。所以计数和等待队列的状态始终一致，而且 oob 和 inband 都能安全调用。

睡眠的实现在宏 `evl_wait_event_timeout`（`include/evl/wait.h`）里：

```
持锁 → 条件不满足 → evl_add_wait_queue（挂到队列上）→ 放锁 → evl_wait_schedule（让出 CPU）
     → 醒来 → 若被广播或出错就退出，否则持锁重新检查条件
```

## 3. 数据结构

```c
struct evl_ksem {
	struct evl_wait_queue wait;   /* EVL 等待队列：内含 hard lock wchan.lock 和等待线程链表 */
	unsigned int value;           /* 计数，只在 wait.wchan.lock 下读写 */
};
```

| 字段 | 含义 | 谁读写 | 保护 |
|---|---|---|---|
| `wait` | 等待者队列。`wait.wchan.lock` 同时充当整个信号量的锁 | 所有函数 | 自身的 hard lock |
| `value` | 可用单位数 | `down_ksem`（读、减）、`evl_up`（加）、`evl_broadcast`（清零）、`evl_init_ksem` | `wait.wchan.lock` |

## 4. 函数语义

| 函数 | 能在哪个 stage 调用 | 语义 |
|---|---|---|
| `evl_init_ksem(ksem, value)` | inband（初始化） | 设初值，初始化等待队列（单调时钟、按优先级排队） |
| `evl_destroy_ksem` | inband | 销毁等待队列 |
| `down_ksem`（static） | 持锁调用 | `value > 0` 则减 1 返回 true，否则返回 false。是 `evl_wait_event` 的条件 |
| `evl_down(ksem)` | **oob**（会睡，调用者必须是 EVL 线程） | 无限期等待一个单位。返回 0，或者被打断时返回负值 |
| `evl_down_timeout(ksem, timeout)` | oob | 同上，带**绝对**超时（`EVL_ABS`）。超时为"非阻塞"值时直接返回 `-EAGAIN` |
| `evl_trydown(ksem)` | 任意 | 不睡：持锁试一次，拿到返回 0，否则 `-EAGAIN` |
| `evl_up(ksem)` | 任意 | 持锁：`value++`，唤醒队首等待者；放锁后 `evl_schedule()`，让被唤醒的高优先级线程立刻运行 |
| `evl_broadcast(ksem)` | 任意 | 持锁：`value = 0`，唤醒全部等待者（`EVL_T_BCAST`）；放锁后 `evl_schedule()` |

一个细节：`evl_up` 只唤醒一个等待者，但不会把刚加的单位"交给"它。被唤醒的线程醒来后要重新执行 `down_ksem`，这期间别人可能先拿走这个单位，于是它会再睡下去。这是常见的"唤醒后重新竞争"写法。


## 5. 可信域划分

按 [README 的规则 1–10](README.md#划分规则)。字段级的原始结果在 `tools/trust-closure/out/subsys/sem.txt`，下面是在它基础上人工推导的类型级结论。

### 5.1 代码：哪些函数可信（规则 1）

**oob 函数**：5 个导出函数在语义上都可以在 oob 上调用（`evl_down*` 要求调用者是 EVL 线程；`trydown/up/broadcast` 也可以在 oob 中断里用），加上它们调用的 `down_ksem`，共 6 个。这是按语义手工定的种子，工具只负责沿调用图往下传播。

**写可信数据的 inband 函数**：`evl_init_ksem`、`evl_destroy_ksem`（`sem.h`）。它们不在 oob 上跑，但写 `value` 和 `wait`，所以也可信。**判据是"写了可信数据"，不是"跑在 oob 上"。**

**调用链向上**：

```
spidev_oob_ioctl  ←  EVL 核心的 oob ioctl 分派（入口：可信）
  └ run_oob_transfer                      oob    → 可信
      └ evl_down / evl_up
spidev_ioctl      ←  VFS（入口：Linux，arg 是用户输入）
  └ enable_oob_mode / disable_oob_mode    inband → 可信（写 ksem）
      └ evl_trydown / evl_up
spidev_open / spidev_release  ←  VFS（入口：Linux）
  └ init_oob_state / release_oob_state    inband → 可信（初始化、销毁 ksem）
```

所以 sem.c 本身虽然不碰 Linux，**调用它的 spidev 路径都会变成可信代码**。spidev 的这些函数还会调用 Linux SPI 核心（`spi_terminate_oob_transfer`、`spi_pulse_oob_transfer` 等），它们是否满足规则 2，要单独分析 spidev。

### 5.2 数据：参数和隐式状态（规则 3、4）

| 来源 | 例子 | 处理 |
|---|---|---|
| 传值参数 | `evl_down_timeout` 的 `timeout`、`evl_init_ksem` 的 `value` | 规则 3：调用者负责，sem 不追 |
| 指针参数 | 所有函数的 `ksem` | 规则 4：依赖 `evl_ksem` 类型，找它的所有写者（5.3） |
| 隐式状态 | 等待宏里读当前线程的 `info`（`EVL_T_BCAST`） | 规则 4：`evl_thread` 是 EVL 核心的类型，固定可信 |
| 栈 | `flags`、`ret` 等局部变量 | 不发布地址，规则 3 的"自己的副本"。线程栈整体是运行时底座（doc 14） |

### 5.3 `evl_ksem` 类型的写者（规则 4）

| 写者 | 写什么 |
|---|---|
| `down_ksem`、`evl_up`、`evl_broadcast` | `value` |
| `evl_init_ksem`、`EVL_KSEM_INITIALIZER`（静态初始化） | `value`、`wait` |
| `evl_destroy_ksem` | `wait` |
| EVL 等待队列核心：`evl_add_wait_queue`、`evl_wake_up_head`、`evl_flush_wait_locked`、`evl_wait_schedule` 等 | `wait.wchan` 的锁、等待链表、`owner`。**对所有 `evl_wait_queue` 实例都一样**，所以 `wait.c` 整体要可信 |
| 分配器（容器的） | 容器被分配和释放时，ksem 所在的内存也随之清零和回收 |

没有任何 Linux 代码直接写 `evl_ksem`。

### 5.4 分配单元：`evl_ksem` 总是被嵌入（规则 5、6、7）

`evl_ksem` **没有自己的分配点**：没有 `kzalloc(sizeof(struct evl_ksem))`，要么用 `DEFINE_EVL_KSEM` 定义成全局变量，要么嵌在别的结构体里。所以"`evl_ksem` 可信"本身不足以确定放哪里，要看容器：

- **嵌入的成员跟随它**（规则 6）：`wait`（`evl_wait_queue` → `evl_wait_channel`）、`value`。`wait` 里的指针 `clock` 指向全局的 `evl_mono_clock`，`owner` 和等待链表指向 `evl_thread`，都是 EVL 核心的可信对象。
- **它跟随容器**（规则 7）：树内唯一的容器是 spidev：

```c
struct spidev_data {          /* kzalloc，Linux SPI 驱动的设备对象 */
	dev_t devt;
	struct mutex spi_lock;    /* Linux 的 mutex，Linux 会写 */
	struct spi_device *spi;
	struct list_head device_entry;   /* 挂在 Linux 的全局链表上 */
	...
	struct {
		struct evl_file efile;
		struct spi_oob_transfer xfer;
		struct evl_flag flag;
		struct evl_ksem sem;     /* ← 这里 */
		bool enabled;
	} oob;
};
```

`spidev_data` 整体不可能进可信域，因为 Linux 的 mutex、链表、`spi` 指针都在里面。按规则 7，可行的改法是**把 `oob` 子结构拆出来单独分配**，放进可信域，`spidev_data` 里只留一个指针。拆出来的 `oob` 里，`efile`、`flag`、`sem` 都是 EVL 类型；`xfer` 是 SPI 核心的 oob 传输描述符，Linux SPI 控制器驱动也会写它，需要单独判断。

对于用 `DEFINE_EVL_KSEM` 定义的全局变量，要把它放进可信的数据段。

### 5.5 Linux 依赖

sem.c 自己**没有**：不读 Linux 对象（规则 9），不调用 Linux 函数（规则 2），没有嵌入 Linux 写的成员（规则 8）。它的 Linux 依赖全部来自**容器和调用者**：spidev 用 `filp->private_data` 找 `spidev_data`（M4），以及上面说的容器问题。

### 5.6 小结

| 问题 | 结论 |
|---|---|
| 可信代码 | sem.c 全部 6 个函数，sem.h 的初始化和销毁，`wait.c`；调用链上的 spidev 函数 |
| 可信类型 | `evl_ksem`，但它不是分配单元，实际放置取决于容器 |
| 需要改的地方 | 不在 sem.c，在容器：spidev 的 `oob` 子结构要单独分配 |

## 6. 值得注意的地方

1. **"不碰 Linux"不等于"划分简单"。** sem.c 是最干净的 EVL 代码，但它是一个被嵌入的类型，划分的难点全部转移到了用它的地方。这正是规则 7 要解决的情况。
2. 第一版说"没有调用者，可以考虑不放进最小 EVL"，这个建议撤回：spidev 的 oob 模式依赖它。
3. `evl_down_timeout` 的超时是**绝对时间**，和 Linux `down_timeout` 的相对时间不同，容易误用。
4. spidev 把 ksem 当互斥锁用（初值 1）。在 inband 上用 `evl_trydown` 而不是 `evl_down`，是因为 inband 上不能在 EVL 等待队列上睡眠；拿不到就把 `evl_trydown` 的 `-EAGAIN` 直接返回给用户。
