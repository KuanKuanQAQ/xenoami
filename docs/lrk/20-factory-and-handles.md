# 20 factory.c 分析，以及可信句柄表

> 2026-11-10。分析和设计稿，**没有改代码**。行号未注明文件的，指 `kernel/evl/factory.c`（961 行，HEAD `daed79c1cd9b`）。
> 相关：`evl_element` 要拆分的结论见 [16-subsystems/README](16-subsystems/README.md)；通道规则 C1–C7 见 [19](19-untrusted-peer-channels.md)；xbuf 实施时留下的、依赖本文的问题见 [18 §16](18-xbuf-split.md)。

---

## 0. 结论

- factory.c 管四件事：element 的生命周期、在 Linux 里的可见性（cdev、sysfs、fd）、file 与 element 的绑定、工厂本身。真正需要可信的只有生命周期，以及"由调用者给出的标识找到 element"。
- **最严重的问题在 oob 系统调用入口**：fd 经 Linux 的 fd 表得到 `struct file`，再调用 `filp->f_op->oob_read`（Linux 对象里的函数指针），进入 fop 以后又用 `element_of(filp)` 读 `filp->private_data`。整条路径都是不可信方可以改写的。
- **决定（2026-11-10）**：不采用"Linux 查到以后再校验"的做法，因为校验挡不住"换成另一个合法对象"。改为**在可信域专门维护一张 per-process 的句柄表**。oob 系统调用只按句柄查这张表；句柄只能由可信代码创建。libevl 的 ABI 随之修改。
- EVL 已经有一张可信的表（namespace 的 fundle 表），但只收了 thread、monitor、observable 三类，而且是全局的、按 fundle 索引，不是 per-process 的句柄表。新表可以复用它的做法（红黑树或数组 + hard spinlock + 查找时加引用）。
- 顺带发现两个和划分无关的 bug：B1（flusher kthread 的指针用错）、B2（`getname_kernel` 的错误检查写错），见 §5。

---

## 1. factory.c 管的四件事

| 职责 | 主要函数 | 运行在 |
|---|---|---|
| element 的生命周期：初始化、引用计数、最后一次 put、销毁 | `evl_init_element` 71、`__evl_put_element` 253、`__do_put_element` 207、`factory_flusher` 820 | 初始化在 inband；**put 可能在 oob 上** |
| 在 Linux 里可见：cdev、sysfs 设备、匿名文件、安装 fd、名字查重 | `create_element_device` 453、`do_element_visibility` 370、`evl_remove_element_device` 556 | inband |
| file 与 element 的绑定 | `evl_open_element` 186、`bind_file_to_element` 144、`evl_release_element` 304 | inband（Linux VFS 回调） |
| 工厂本身：class、chrdev 号段、clone 设备 ioctl | `create_factory` 749、`ioctl_clone_device` 571 | inband |

---

## 2. `struct evl_element`（`include/evl/factory.h:66-84`）

| 字段 | 是什么 | oob 用吗 | 初步归属 |
|---|---|---|---|
| `ns_node` | 引用计数 `refs` + fundle 表节点（`include/evl/map.h:24-31`） | **用**：get/put、按 fundle 查找 | 可信 |
| `clone_flags`、`factory`、`scope`、`minor` | 属性 | 读 | 可信 |
| `rcu` | `rcu_head`，给 `kfree_rcu` 用 | 否 | Linux 对象，见 §4 |
| `cdev` | 字符设备，Linux 的 `inode->i_cdev` 指向它 | 否 | 不可信：Linux 一直持有它的指针 |
| `dev` | sysfs 设备，`drvdata` 反指 element | 否 | 不可信 |
| `devname` | `struct filename`，由 `getname` 分配 | 否 | 不可信；可信侧需要名字时另存一份 |
| `hash` | 名字查重表节点，由 Linux mutex 保护（`:471-481`） | 否 | 不可信 |
| `fpriv.filp` / `efd` | 私有 element 的匿名文件和 fd | 否 | 不可信 |
| `irq_work` | oob 上 put 时的跳板（`:287-288`） | **用** | 换成通知中继（18 §7.4） |
| `flush` | 挂在 flusher 队列上，由 Linux 等待队列的锁保护（`:244-250`） | 否 | 见 §4 |
| `owned` | 挂在 Dovetail 的 `oob_mm_state->elements` 上，进程退出时统一 put（`thread.c:390-393`） | 否 | 可信（per-process 状态，见 §6.4） |

每个 EVL 对象都嵌着 `evl_element`，所以每个对象里都嵌着 `cdev`、`device`、`rcu_head`、`irq_work`。

---

## 3. 由 Linux 指针得到可信指针的地方

| # | 路径 | 位置 | 运行在 |
|---|---|---|---|
| L1 | **oob 系统调用**：fd → Linux fd 表（`CLASS(fd)`）→ `filp->f_oob_state.data` → `evl_file`；**调用 `filp->f_op->oob_read`**；fop 里再 `element_of(filp)` → `filp->private_data` → binding → element | `file.c:34-49`、`syscall.c:46-64`（write、ioctl 同理）、`factory.h:25-29` | **oob** |
| L1' | 内核里按 fd 查文件：poll 加监视项、clock、`evl_get_xbuf`（latmus） | `poll.c:190`、`:241`、`:482`；`clock.c:463`；`xbuf.c` 的 `evl_get_xbuf`；`latmus.c:790` | oob 或 inband |
| L2 | open：`inode->i_cdev` → `container_of` 得到 element | `:191` | inband |
| L3 | sysfs：`dev_get_drvdata(dev)` → element | `factory.h:132-137` | inband |
| L4 | clone 设备：`filp->f_inode->i_cdev` → factory；`filp->private_data` 存 element | `:574`、`:607`、`:657` | inband |

L1 在 oob 热路径上连续经过三个 Linux 侧的指针，还调用了一个 Linux 侧的函数指针，违反了 C3(c)，也违反了"可信代码不调用不可信代码"。

**为什么不用校验（被否决的方案 B）**：Linux 查到 `filp` 以后，可信侧核对 binding 是否在自己的表里，函数指针改从可信的 `e->factory` 取。这样能保证内存安全，但 Linux 仍然可以把 fd 换成**另一个合法的** element，让实时线程操作错误的对象。元素是否找对，本身就要由可信域决定。

---

## 4. 释放路径

```
evl_put_element（可能在 oob 上）── 引用计数到 0
  └ __evl_put_element（:253）
      ├ oob 上或关着硬中断：irq_work_queue → do_put_element_irq（Linux irq_work）
      │      → 挂进 flusher_queue（Linux 等待队列的锁）→ 唤醒 evl_flusher（Linux kthread）
      ├ 在 workqueue 里：同样走 flusher
      └ 其他情况：直接调用
  → __do_put_element（:207）
      ├ evl_remove_element_device：device_unregister、cdev_del、从名字表删除   ← Linux 的拆除
      └ fac->dispose(e)：销毁 EVL 等待对象 … kfree_rcu(e)                     ← 可信的拆除
```

- 可信对象什么时候被释放，由 Linux 驱动：irq_work、kthread，最后还要等 RCU。
- 要等 RCU，是因为 `cdev` 嵌在 element 里，`__fput` 在 release 之后还要用它（`:216-220`）。`cdev` 移到不可信对象里以后，可信对象只需要等自己的引用计数归零。18 §16 第 5 条（xbuf 销毁时靠 RCU 宽限期兜底）也随之解决。
- `__do_put_element` 要拆成两步：Linux 侧先撤销可见性，可信侧再销毁。顺序不能反（规则 C7）。

`evl_file` 有同样的模式：`evl_up_crossing` 在 oob 上调用 `irq_work_queue`，`crossing` 里嵌着 Linux 的 `completion`（`include/evl/crossing.h:32-73`）。

---

## 5. 顺带发现的问题（读代码得出，未运行验证）

| # | 问题 | 位置 |
|---|---|---|
| B1 | `kthread_run` 的返回值 `p` 没有保存，失败路径和清理时调用的却是 `kthread_stop(&flusher_kthread)`。`flusher_kthread` 是全零的静态 `task_struct`，不是真正的线程。只在初始化失败或清理时才会走到 | `:65`、`:921`、`:935`、`:948` |
| B2 | `getname_kernel` 出错时返回 `ERR_PTR`，这里检查的却是 `devname == NULL`。出错时会把 `ERR_PTR` 存进 `e->devname` | `:543-547` |
| B3 | `return ret;;` | `:887`，没有影响 |

---

## 6. 可信句柄表

### 6.1 句柄表管的是"oob 能力"，不只是 element

现在带 oob 操作的文件不止 EVL element：`.oob_read` 等的实现者共 17 个文件，包括 EVL 自己的 element（xbuf、proxy、monitor、observable、thread、clock、poll 等）、`net/socket.c` 的 socket、`drivers/spi/spidev.c`、`drivers/gpio/gpiolib-cdev.c`，以及 `drivers/evl/` 里的 latmus、hectic。

所以表项指向的是一个**可信的 oob 对象**：对象指针，加上这个对象类型的 oob 操作表。element 只是其中一种。驱动和 socket 是否可信，是另一个问题（§8 的 H5）。

### 6.2 结构

```c
/* 可信：每个类型一份，放在只读数据里，代替 file_operations 里的 oob_* */
struct evl_oob_ops {
	ssize_t (*read)(struct evl_handle *h, char __user *u_buf, size_t count);
	ssize_t (*write)(struct evl_handle *h, const char __user *u_buf, size_t count);
	long (*ioctl)(struct evl_handle *h, unsigned int cmd, unsigned long arg);
	__poll_t (*poll)(struct evl_handle *h, struct oob_poll_wait *wait);
};

/* 可信：一个表项 */
struct evl_handle {
	void *obj;                      /* 例如 struct evl_element * */
	const struct evl_oob_ops *ops;  /* 来自可信的工厂，不来自 filp->f_op */
	unsigned int mode;              /* 读 / 写权限，创建时定下 */
	unsigned int flags;             /* 例如 O_NONBLOCK 的可信镜像（18 Q7） */
	struct evl_crossing crossing;   /* oob 引用，代替 evl_file 里的那份 */
	u32 gen;                        /* 代数，防止旧句柄命中新表项 */
};

/* 可信：每个进程一张 */
struct evl_handle_table {
	hard_spinlock_t lock;
	unsigned int size;
	struct evl_handle **slots;
};
```

句柄的编码：`handle = (gen << IDX_BITS) | idx`。查找时按 `idx` 取表项，比较 `gen`，再加 oob 引用，整个过程在表锁内完成。这和 `evl_get_map_node`（`lib/map.c:134-149`）的做法一样。

### 6.3 oob 系统调用

```c
static __EVL_SYSCALL(read, (int handle, char __user *u_buf, size_t size))
{
	struct evl_handle *h = evl_get_handle(handle);  /* 只查可信表 */
	ssize_t ret;

	if (h == NULL)
		return -EBADF;
	if (!(h->mode & EVL_H_READ))
		ret = -EBADF;
	else if (h->ops->read == NULL)
		ret = -EINVAL;
	else
		ret = h->ops->read(h, u_buf, size);
	evl_put_handle(h);
	return ret;
}
```

oob 的 fop 不再接收 `struct file *`，而是接收 `struct evl_handle *`，直接从 `h->obj` 取对象。`element_of(filp)` 从所有 oob 路径上消失。inband 的 fop 不变，仍然用 `filp->private_data`，因为 Linux 骗自己没有意义（18 §10.1）。

### 6.4 "当前进程的表"从哪来

查表要先找到当前进程的表，这一步也不能经过 Linux：

- 现在的 `evl_current()` 读 Dovetail 放在 `thread_info` 里的 `oob_state.thread`（`include/evl/thread.h:228-231`），是 Linux 对象里的指针。
- oob 上可以改用调度器的状态：`this_evl_rq()->curr`。这是 EVL 自己的运行队列，由 EVL 调度器维护。当前 EVL 线程的可信结构里存一个指向进程表的指针，进程表在进程第一次绑定 EVL 时由可信代码创建。
- 剩下的问题：per-CPU 区的寻址仍然经过 Linux 的 `this_cpu_off`（doc 11 #7），属于共性问题，这里不展开。

`owned` 链表（现在挂在 Dovetail 的 `oob_mm_state` 上）也改挂到这张可信的进程表上。

### 6.5 句柄从哪里来

句柄只能由可信代码创建，并直接交给用户态。Linux 可以不配合（不转交、不清理），但不能指定句柄指向哪个对象。

| 场景 | 现在 | 改后 |
|---|---|---|
| 创建私有 element（clone ioctl） | `fac->build` → 匿名文件 → 安装 fd → 把 `efd` 写回 `req` | `build` 之后由可信代码在调用者的表里装一个句柄，`req` 里同时返回 `efd`（给 inband 用）和 `handle` |
| 打开公开 element（`/dev/evl/xbuf/foo`） | Linux `open` → `i_cdev` → element（L2） | **按名字在可信侧解析**：新增一个 EVL 调用 `attach(factory, name, mode)`，可信侧在自己的名字表里查找，装句柄。Linux 的 `open` 只用于得到 inband 的 fd |
| 线程自己的句柄（`__evl_current_efd`） | 绑定线程时得到的 fd | 绑定时由可信代码装句柄 |
| 内核里按 fd 查找（L1'） | `evl_get_file(fd)` | 改成按句柄查找，用户态传入的是句柄 |

需要可信的名字表：现在的名字查重表在 factory 里，用 Linux 的 mutex 保护，名字字符串是 Linux 的 `struct filename`。可信侧要有自己的一份：名字字符串拷贝 + 哈希表 + EVL 的锁。Linux 侧的设备名仍然可以保留，只给 `/dev`、sysfs 用。

### 6.6 句柄和 fd 的关系

| 事件 | 处理 |
|---|---|
| `close(fd)` | 不影响句柄。libevl 的销毁函数同时关闭两者：`close(fd)` + 新的 `evl_close_handle(handle)` |
| `dup(fd)` | 句柄不随之复制。如果需要，新增 `evl_dup_handle` |
| `fork` | 子进程没有父进程的句柄表，需要重新 attach。现在 fd 会被子进程继承，这是语义上的变化 |
| 进程退出 | 可信侧清空进程表，逐个 put。触发点是 Linux 的退出钩子，Linux 不调用的话就是泄漏（活性问题），不影响安全 |

### 6.7 和方案 A1 的比较

还有一种只改一半 ABI 的做法（A1）：句柄的数值就等于 fd，可信表以 fd 号为键，但内容由可信侧维护。好处是应用里的 `oob_read(fd, ...)` 不用改。

坏处是表项只能在 Linux 的 `open` 或 `clone` 回调里建立，所以 fd 号和 element 的对应关系仍然由 Linux 告诉可信侧，这又回到了"Linux 决定指向哪个对象"。既然你说 libevl 可以改，就不采用 A1。

---

## 7. 实施顺序

| 步骤 | 内容 | 改变 ABI？ |
|---|---|---|
| 0 | 修 B1、B2 | 否 |
| 1 | 可信句柄表的基础设施：`evl_handle`、`evl_handle_table`、`evl_get_handle` / `evl_put_handle`，挂在当前进程的可信结构上（先用 `evl_current()` 找，§6.4 第二阶段再换） | 否 |
| 2 | clone ioctl 和线程绑定时同时返回句柄；oob 系统调用接受句柄（过渡期用一个标志位区分 fd 和句柄）；libevl 改用句柄 | 是 |
| 3 | `evl_oob_ops` 代替 `file_operations` 的 `oob_*`，先改 xbuf、proxy，再改其余 element；oob 路径上删除 `element_of` | 否（内核内部） |
| 4 | 内核里按 fd 查找的地方（L1'）改成按句柄查找 | 是（poll、clock 的 ioctl 参数） |
| 5 | 可信名字表和 `attach`，公开 element 不再经由 `open` 取得 oob 能力 | 是 |
| 6 | 拆 `evl_element`：`cdev`、`device`、`filename`、`hash`、`fpriv` 移到不可信对象；`__do_put_element` 拆成两步；put 改用通知中继；去掉 `kfree_rcu` | 否 |
| 7 | 去掉过渡期的 fd 路径 | 是 |

每一步之后都跑 libevl 的全部测试，基线用 18 §16 的结果（67 通过、10 失败）。

---

## 8. 待确认的问题

| # | 问题 | 说明 |
|---|---|---|
| H1 | 公开 element 的访问控制 | 现在靠 Linux 的文件权限，`evl_may_access_factory` 读的是 Linux 的 `cred`（`:807-812`）。按名字 attach 以后，可信侧需要自己的规则，而"调用者是谁"这个身份本身也来自 Linux |
| H2 | 句柄是 per-process 还是 per-thread | 建议 per-process，和现在 fd 的语义一致 |
| H3 | 过渡期怎样区分 fd 和句柄 | 例如句柄的最高位置 1。fd 不会是负数，所以不会和句柄冲突 |
| H4 | `evl_current()` 换成调度器的 `curr` | 只在 oob 上成立；inband 上调用 oob 系统调用的非 EVL 线程（log 11-05）没有 `curr`，需要另外处理 |
| H5 | 驱动（spidev、gpio、latmus、hectic）和 socket 是否进可信域 | 决定它们能不能有句柄。不进的话，它们的 oob 操作要么去掉，要么另外设计 |
| H6 | `dup`、`fork` 的语义变化 | libevl 的 `file-dup`、`clone-fork-exec` 测试会受影响，需要一起改 |
