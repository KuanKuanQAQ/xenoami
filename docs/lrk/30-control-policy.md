# 30 谁能控制 EVL 线程：只有 EVL 线程

本文可以单独阅读。它回答 doc 28 §5.2 留下的问题："普通 Linux 线程能不能改变 EVL 线程的运行？"并说明实现、影响和剩下的缺口。

代码位置都指 `~/xenoami/linux-evl`。

---

## 0. 结论

**安全假设。** 实时系统里的实时任务就是 EVL 线程。它们直接影响物理世界，是系统的核心，系统维护者要保证它们本身可信、安全。我们做的是给这些任务一个安全运行的平台。所以：

- **EVL 线程都可信**，它们之间互相控制（改调度、挂起、降级……）是允许的；
- **不是 EVL 线程的任务（普通 Linux 线程、shell、管理脚本）不可信**，只能查询，不能改变 EVL 线程怎么运行。

**规则。** 所有"改变 EVL 线程的调度、时间或生死"的请求，不管从哪个接口进来（inband ioctl、sysfs），都要求调用者是 EVL 线程：

```c
static __always_inline bool evl_may_control(void)   /* include/evl/thread.h:254 */
{
	return evl_current() != NULL;
}
```

oob ioctl 不用检查，因为只有 EVL 线程能发出 oob 请求。

**改了 8 处**（§4）：线程的 7 个控制 ioctl、TP 调度表、quota 组、quota 周期、时钟 gravity（sysfs 和 latmus）、EVL 时钟的设时、停核心。

**没改的**（§6）：Linux 自己对任务的控制（信号、ptrace、改亲和性）、数据通道（monitor、observable、xbuf、proxy）、查询类请求。

---

## 1. 原来的语义：拿到 fd 就有权限

### 1.1 EVL

每个 EVL 线程都是一个 element，对应一个文件，`file_operations` 里有 `unlocked_ioctl`（inband）和 `oob_ioctl`（oob）。拿到 fd 的途径：

| 途径 | 说明 |
|---|---|
| 线程自己 | `evl_attach_self()` 返回自己的 fd |
| 同进程的其他线程 | 共享 fd 表 |
| 跨进程 | `SCM_RIGHTS` 传递、fork 继承 |
| public 线程 | 任何人都可以打开 `/dev/evl/thread/<名字>`，受设备节点的权限位约束 |

拿到 fd 之后，`thread_common_ioctl()` **不检查调用者是谁**：

| 命令 | 效果 |
|---|---|
| `EVL_THRIOC_SET_SCHEDPARAM` | 改调度类和优先级 |
| `EVL_THRIOC_SET_MODE` / `CLEAR_MODE` | 改模式位（健康监视选项） |
| `EVL_THRIOC_UNBLOCK` | 打断它当前的等待 |
| `EVL_THRIOC_DEMOTE` | 降级为 weak 类并踢回 inband |
| `EVL_THRIOC_SUSPEND` / `RESUME` | 挂起、恢复 |

这些命令 inband 和 oob 都能发。libevl 的 `evl_set_schedattr()` 等函数用 `__evl_transparent_call()`：调用者在 inband 就走 `ioctl()`，在 oob 就走 `oob_ioctl()`。没有 attach 的普通线程永远走 inband。

### 1.2 Linux

Linux 线程原本没有 fd，控制它用的是 pid 加系统调用，**每次都检查调用者的凭证**：

| 操作 | 系统调用 | 权限检查 |
|---|---|---|
| 发信号 | `kill`、`tgkill` | 同 uid，或 `CAP_KILL` |
| 改调度 | `sched_setscheduler`、`setpriority`、`sched_setaffinity` | 同 uid，或 `CAP_SYS_NICE` |
| 调试 | `ptrace` | `ptrace_may_access()` + LSM（如 Yama） |

后来有了 pidfd（`pidfd_open`、`CLONE_PIDFD`，6.9 起有指向单个线程的 `PIDFD_THREAD`），但 pidfd 只是一个稳定的引用，避免 pid 复用。`pidfd_send_signal()` 仍然做和 `kill` 一样的凭证检查。**Linux 里拿到 pidfd 不等于有权限。**

所以"拿到 fd 就能控制"是 EVL 自己的设计。在我们的安全假设下它不够：一个普通进程拿到 public 线程的 fd，就能把实时线程挂起或降级。

---

## 2. 为什么用 `evl_current()` 判断

`evl_current()` 读的是 `dovetail_current_state()->thread`，即当前任务在 Dovetail 里的 oob 状态。这个字段只有 EVL 核心在 attach / detach 时写，属于信任基（doc 23 的 Y8）。

- 它回答的正是"调用者是不是 EVL 线程"，和安全假设一一对应；
- 不依赖 Linux 的 uid、capability，这些是 z 的值；
- 同一个进程里，attach 过的线程可以控制，没 attach 的线程不行，粒度是线程。

**它不区分进程。** 另一个进程里的 EVL 线程拿到 fd，同样可以控制。按安全假设，EVL 线程都可信，这正是想要的行为（doc 28 的方案 ③"只允许同进程"没有采用）。

---

## 3. 盘点：z 能从哪些入口影响 EVL 线程

| 入口 | 请求 | 影响 | 处理 |
|---|---|---|---|
| 线程 fd，inband ioctl | SET_SCHEDPARAM、SET/CLEAR_MODE、UNBLOCK、DEMOTE、SUSPEND、RESUME | 直接改这个线程 | **加检查**（§4.1） |
| 线程 fd，inband ioctl | GET_SCHEDPARAM、GET_STATE | 只读 | 不变 |
| 线程 fd，inband ioctl | JOIN、DETACH_SELF、SWITCH_INBAND | 等待它结束 / 只作用于自己 | 不变 |
| 线程 fd，inband ioctl | 订阅线程的 observable | 接收它的健康通知 | 不变（只读） |
| 控制设备，`EVL_CTLIOC_SCHEDCTL` | TP 装/卸/启/停调度表；quota 加/删/设组 | 改某个 CPU 上所有 TP、quota 线程的运行时间 | **加检查，`get` 除外**（§4.2） |
| 控制设备，`EVL_CTLIOC_GET_CPUSTATE`、`GET_COREINFO` | 查询 | 只读 | 不变 |
| sysfs `evl/control/quota_period` | 写 | 改所有 quota 组的周期 | **加检查**（§4.3） |
| sysfs `evl/control/state` | `stop` | 取消所有 EVL 用户线程 | **有用户线程时拒绝**（§4.4） |
| sysfs `evl/control/state` | `start` | 核心停着时才有效，此时没有 EVL 线程 | 不变 |
| sysfs `clock/<名字>/gravity` | 写 | 改所有 EVL 定时器的提前量 | **加检查**（§4.5） |
| `/dev/latmus` | `EVL_LATIOC_RESET`、`TUNE` | 重置或校准 gravity | **加检查**（§4.5） |
| 时钟 fd，inband ioctl | `EVL_CLKIOC_SET_TIME` | 移动该时钟上所有绝对超时 | **加检查**（§4.6） |
| monitor、observable、xbuf、proxy | 读写、post | 交换数据、唤醒等待者 | 不变，数据通道（§6.2） |
| Linux 系统调用 | 信号、ptrace、`sched_setaffinity`、退出 | Linux 直接操作这个任务 | 不变，EVL 管不了（§6.1） |
| sysfs `net/*` | VLAN、路由、ARP | EVL 网络栈配置 | 不变，当前配置不编译网络栈（doc 24 N6） |

---

## 4. 改动

### 4.1 线程 ioctl（`thread.c:2334` 起的 `__thread_ioctl`）

```c
	case EVL_THRIOC_SET_SCHEDPARAM:
	case EVL_THRIOC_SET_MODE:
	case EVL_THRIOC_CLEAR_MODE:
	case EVL_THRIOC_UNBLOCK:
	case EVL_THRIOC_DEMOTE:
	case EVL_THRIOC_SUSPEND:
	case EVL_THRIOC_RESUME:
		if (!evl_may_control())
			break;              /* ret 初值就是 -EPERM */
		fallthrough;
	default:
		ret = thread_common_ioctl(thread, cmd, arg);
```

只改 inband 入口。`__thread_oob_ioctl` 不用改。compat 入口也走 `__thread_ioctl`，一并覆盖。

### 4.2 调度控制（`control.c:176`、`219`）

TP 和 quota 的参数先拷进内核，再按操作码判断：

```c
	if (param.quota.op != evl_quota_get && !evl_may_control())
		return -EPERM;
	...
	if (_param.tp.op != evl_tp_get && !evl_may_control())
		return -EPERM;
```

`get` 只读，任何人可以查。检查放在 `do_quota_control` / `do_tp_control` 里，因为操作码在参数块里，`control_common_ioctl` 看不到。oob 路径也经过这里，但 oob 调用者一定是 EVL 线程，检查总是通过。

### 4.3 quota 周期（`control.c:532`）

`quota_period_store` 开头检查。

### 4.4 停核心（`control.c:82-95`）

原来 `echo stop > /sys/devices/virtual/evl/control/state` 会调用 `evl_killall(EVL_T_USER)`，取消所有 EVL 用户线程并等它们退出。`evl_killall` 本身已经拒绝 EVL 线程调用（`if (evl_current()) return -EPERM`），所以原来**只有非 EVL 线程能停核心，而且一停就杀掉所有实时任务**，正好和安全假设相反。

现在：

```c
	case EVL_STATE_RUNNING:              /* cmpxchg 已把状态改成 TEARDOWN */
		if (evl_count_threads(EVL_T_USER) > 0) {
			set_evl_state(state);        /* 恢复 RUNNING */
			return -EBUSY;
		}
		ret = evl_killall(EVL_T_USER);
```

- 还有 EVL 用户线程时，`stop` 返回 `-EBUSY`，什么都不做。实时任务要自己退出，核心才能停。
- 没有用户线程时照常停（EVL 内核线程是核心自己的服务，跟着停）。
- 竞争窗口：状态改成 TEARDOWN 之后，`ioctl_clone_device()` 不再创建新 element（`factory.c:593` 检查 `evl_is_running()`）。只有一个在此之前已经通过检查、正在创建中的线程会漏过计数，后面的 `evl_killall` 仍会把它收掉。这个线程还没开始运行，影响可以忽略。
- 新增 `evl_count_threads(mask)`（`thread.c:1485`），在 `thread_list_lock` 下计数，跳过 ROOT 线程。

### 4.5 gravity（`clock.c:990`、`latmus.c:1047`）

gravity 是 EVL 定时器的提前量，改它会让所有实时线程的唤醒时间整体偏移。两个入口：

- sysfs `/sys/devices/virtual/clock/<名字>/gravity` 的 `gravity_store`；
- latmus 驱动的 `EVL_LATIOC_RESET`（恢复出厂值）和 `EVL_LATIOC_TUNE`（校准后写入）。

`EVL_LATIOC_MEASURE`、`RUN`、`PULSE` 只是测量，不改 gravity，不检查。

### 4.6 设时（`clock.c:828`）

`EVL_CLKIOC_SET_TIME` 在 `clock_common_ioctl` 里，inband 和 oob 共用，检查放在 case 里。`GET_TIME`、`GET_RES` 不检查。

在当前配置下，内建的 monotonic / realtime 时钟本身不支持设时（EVL 线程调用得到 `-EOPNOTSUPP`），EVL 的 realtime 时钟跟随 Linux 的 `CLOCK_REALTIME`。所以这条检查现在只对将来注册的、可设时的外部时钟起作用；而 Linux 用 `clock_settime`（需要 `CAP_SYS_TIME`）改系统时间，会间接移动 EVL realtime 时钟上的绝对超时，这属于 §6.1 的"Linux 自己的操作"。

---

## 5. 对用户态的影响

| 场景 | 原来 | 现在 |
|---|---|---|
| 已 attach 的线程调用 `evl_set_schedattr` 等（不管它此刻在 oob 还是 inband） | 成功 | 成功 |
| 没 attach 的线程调用 `evl_set_schedattr`、`evl_demote_thread`、`evl_suspend_thread`…… | 成功 | `-EPERM` |
| 没 attach 的线程调用 `evl_get_schedattr`、`evl_get_state` | 成功 | 成功 |
| 没 attach 的线程调用 `evl_control_sched()` 改 TP / quota | 成功 | `-EPERM`（`get` 仍成功） |
| shell 写 gravity、quota_period | 成功 | `-EPERM` |
| `evl-stop`（shell 写 `stop`），有 EVL 用户线程在跑 | 杀掉它们，停核心 | `-EBUSY` |
| `evl-stop`，没有 EVL 用户线程 | 停核心 | 停核心 |
| `latmus -r`（先 reset 再 attach） | 成功 | `-EPERM`，要改成 attach 之后再 reset |
| `latmus -t`（校准） | 成功 | 成功（libevl 在 TUNE 之前已 attach，`latmus.c:871`） |
| `evl-ps`、`evl-check`、读 sysfs | 成功 | 成功 |

libevl 自带的测试里，只有 `sched-tp-accuracy` 的 main 线程没 attach 就改 TP 调度表，已在测试里补上 attach（§8.3）；其余测试都先 `evl_attach_self()` 再调用控制函数。

**管理工具要怎么写。** 需要调整实时线程的管理程序，自己先 `evl_attach_self()` 成为 EVL 线程。attach 走的是 `/dev/evl/thread/clone` 上的 clone 请求，能不能 attach 由这个设备节点的权限决定。这和安全假设一致：能控制实时任务的程序本身就是"实时系统的一部分"，由维护者保证可信。如果担心任意进程都能 attach，下一步要做的是"谁能 attach"的策略，见 §7。

---

## 6. 没改的，以及为什么

### 6.1 Linux 自己对任务的控制

EVL 线程就是一个 Linux 任务（doc 28 §5.1）。下面这些由 Linux 执行，EVL 只在事后收到 Dovetail 事件：

| 操作 | EVL 看到的 | 后果 |
|---|---|---|
| `kill(SIGSTOP / SIGKILL)` | `INBAND_TASK_SIGNAL`、`INBAND_TASK_EXIT`（`thread.c:2016-2019`） | 线程被踢回 inband、停住或退出 |
| `ptrace` | `INBAND_TASK_PTSTOP` / `PTCONT` | 线程被调试器停住 |
| `sched_setaffinity` | `INBAND_TASK_MIGRATION`（`thread.c:1802`） | EVL 跟着迁移线程 |
| `munmap`、OOM killer、cgroup freezer | 无事件或只有退出事件 | 地址空间或生死被改变 |
| `clock_settime(CLOCK_REALTIME)` | 无 | EVL realtime 时钟上的绝对超时跟着移动 |

这些请求根本不经过 EVL，`evl_may_control()` 拦不住。Linux 用凭证检查它们（§1.2）：调用者要和实时任务同 uid，或者有相应 capability。

在我们的边界里（Z 只保证 Z 的内存和状态不被破坏，doc 26），这些操作不会破坏 Z，但会影响实时任务能不能按时运行。目前能做的是 Linux 侧的部署约束：

- 实时任务用专门的 uid 运行，其他程序不用这个 uid；
- 不给其他程序 `CAP_KILL`、`CAP_SYS_NICE`、`CAP_SYS_PTRACE`；
- `kernel.yama.ptrace_scope=3`，禁止 ptrace；
- 实时进程 `mlockall`（EVL 本来就要求）。

要在 Z 里拦住它们，就得让 Dovetail 在 Linux 执行这些操作之前问 EVL，并允许 EVL 否决。这要改 Dovetail 和 Linux 的信号、ptrace、亲和性代码路径，而且否决仍然要靠 Linux 照办，是一个单独的课题。

### 6.2 数据通道

monitor（信号量、事件标志）、observable、xbuf、proxy 是 EVL **有意**提供的跨域同步和通信点（doc 22、doc 25）。普通线程往里面写数据、post 信号量，确实会唤醒 EVL 线程，但这是 EVL 程序自己选择共享这个对象：

- private 对象只有本进程拿得到 fd；
- public 对象是程序自己公开的；
- doc 22 已经规定：从不可信端来的数据只是数据，EVL 一侧自己校验，不依赖它的正确性。

所以这些不算"控制"，不加检查。

### 6.3 查询

GET_SCHEDPARAM、GET_STATE、TP / quota 的 `get`、sysfs 读取、`evl-ps`，都只读，任何人可以查。sysfs 读取已经改成门（doc 28 §4.1），z 不持有 Z 的锁。

---

## 7. 剩下的问题

1. **谁能 attach。** 现在任何进程都能 `evl_attach_self()` 成为 EVL 线程，从而得到控制权限。安全假设是"EVL 线程都可信"，那么"谁能成为 EVL 线程"就是信任边界。可选做法：
   - 由维护者用设备节点权限控制 `/dev/evl/control` 和 `/dev/evl/thread/clone` 的打开（部署层，不改代码）；
   - 在 Z 里维护一张允许 attach 的列表（例如按可执行文件的度量值），这是更大的课题。
2. **Linux 对任务的直接控制**（§6.1）。
3. **`latmus -r`** 要在 libevl 里把 reset 挪到 attach 之后，没改 libevl。
4. **doc 28 的 B4、B5** 仍未修。

---

## 8. 测试

本次和 doc 29 的通知改动一起编译、一起测试。编译无新告警（objtool 的 notrack 告警仍是 101 行，只是地址变了）。

### 8.1 回归（QEMU 配置，未开 TP/QUOTA）

78 个测试通过 74 个，与基线相同。失败的 4 个也相同：`ptrace-sync`（initramfs 里没有 gdb）、`sched-quota-accuracy`、`sched-tp-accuracy`、`sched-tp-overrun`（这三个调度类没编进内核）。dmesg 没有 WARNING / BUG。

通知中断数：整轮测试共 32 万次左右（4 个 CPU 合计），和上一轮（doc 28，36 万次，含 40 秒 sysfs 并发）同一量级。去掉 `waitqueue_active` 之后，中断数没有明显增加。

### 8.2 权限测试（草稿区 `tests/ctl-perm.c`）

子进程 attach 一个 public EVL 线程，父进程打开 `/dev/evl/thread/<名字>`，先以普通线程身份、再 attach 成 EVL 线程，把每个请求各做一遍：

| 请求 | 普通线程 | EVL 线程 |
|---|---|---|
| GET_SCHEDPARAM、GET_STATE | 成功 | 成功 |
| SET_SCHEDPARAM、SET/CLEAR_MODE、SUSPEND、RESUME、UNBLOCK、DEMOTE | `-EPERM` | 成功 |
| sysfs gravity 写回原值 | `-EPERM` | 成功 |
| sysfs quota_period 写回原值 | `-EPERM` | 成功 |
| realtime 时钟 SET_TIME | `-EPERM` | `-EOPNOTSUPP`（内建时钟不支持设时，见 §4.6） |
| SCHEDCTL tp get | 成功 | 成功 |
| SCHEDCTL tp stop | `-EPERM` | 成功 |
| SCHEDCTL quota get（不存在的组） | `-EINVAL`（通过了权限检查） | `-EINVAL` |
| SCHEDCTL quota add / remove | `-EPERM` | 成功 |
| `echo stop > control/state`（子进程的 EVL 线程还活着） | `-EBUSY` | — |

另外在 shell 里：`echo 1000 > .../gravity` 得到 `Operation not permitted`；所有 EVL 用户线程退出后，`echo stop` 成功、`echo start` 成功，之后 `basic-xbuf` 照常通过。

### 8.3 打开 TP/QUOTA 的内核（`build/linux-evl-qemu-sched`）

另编一个打开 `CONFIG_EVL_SCHED_TP`、`CONFIG_EVL_SCHED_QUOTA` 的内核，跑全部测试和权限测试，权限测试全部符合预期（上表中 quota、TP 相关的行就是在这个内核上得到的）。

全部测试中 `sched-tp-accuracy` 失败：它的 main 线程**没有 attach** 就调用 `evl_control_sched()` 安装 TP 调度表，被新规则拒绝。这正是 §5 说的"管理程序要先 attach"。按 `sched-tp-overrun` 的写法，在 main 里安装调度表前加一行 `evl_attach_self()`（`libevl/tests/sched-tp-accuracy.c`）。之后三个 sched 测试各跑 3 遍：

| 测试 | 结果 |
|---|---|
| `sched-tp-accuracy` | 3/3 通过 |
| `sched-tp-overrun` | 3/3 通过 |
| `sched-quota-accuracy` | 3/3 失败，退出码 43，精度 107.8%–109.7% |

`sched-quota-accuracy` 的 main 已经 attach，所有 `evl_control_sched()` 调用都成功了，失败发生在最后的精度判断：它用循环次数衡量 CPU 时间份额，在 QEMU 里偏差超过阈值。我没有在未改动的内核上用同样配置跑过这个测试作对照，所以只能说"失败与权限无关"，不能断定基线下它会通过。

---

## 9. 代码索引

| 内容 | 位置 |
|---|---|
| `evl_may_control()` | `include/evl/thread.h:241-257` |
| `evl_count_threads()` | `kernel/evl/thread.c:1481-1502`，声明 `include/evl/thread.h:364` |
| 线程 inband ioctl 检查 | `kernel/evl/thread.c:2334` 起，检查在 `2371` |
| quota / TP 控制检查 | `kernel/evl/control.c:176`、`219` |
| 停核心 | `kernel/evl/control.c:82-95` |
| quota 周期 | `kernel/evl/control.c:532` |
| 设时 | `kernel/evl/clock.c:828` |
| gravity sysfs | `kernel/evl/clock.c:990` |
| latmus reset / tune | `drivers/evl/latmus.c:1041-1048` |
| 测试程序 | 草稿区 `tests/ctl-perm.c`（不在仓库里） |
| libevl 测试修改 | `libevl/tests/sched-tp-accuracy.c`：main 先 attach |
