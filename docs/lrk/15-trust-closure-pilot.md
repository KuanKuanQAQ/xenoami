# 15 以 oob 依赖为中心的划分：在 proxy 上的第一次试做

> **2026-11-09 补记**：本文第一版有两处问题。(1) §5.2 和 §7 把 05 引入的 `proxy_gate_*` 预设为"会校验的门"再做分析，是循环论证，§7"复现了 01/02/05"的说法不成立；工具只能说明原始代码里存在哪些依赖，不能证明 05 是对的修法。(2) 之后工作区已回退到原始代码（修改存为补丁：`~/xenoami/patches/2026-11-09-shared-state-wip/`），分析转向别的子系统，见 [16-subsystems/](16-subsystems/README.md)。

> **状态**：2026-11-09。工具是第一版，结果是自动分析加人工解读，**没有逐条人工核实**；分析范围只覆盖 proxy 及其直接依赖（§2.3）。
> 工具和全部输出：`~/xenoami/tools/trust-closure/`（`run_extract.sh` 一键重跑，约 1 分半）。

---

## 0. 方法和结论

**方法**（2026-11-09 讨论确定，见 doc 14 之后的讨论）：

1. 要保证 oob 上的运行正确，一方面代码不能被篡改，另一方面 **oob 读的数据（字段）都必须放在可信域**。
2. 确定了这些字段之后，再找出**所有写它们的函数**，以及这些函数为了算出写入值所读的数据。
3. 把这些写函数放进可信域。继续往回追会碰到 Linux 的函数，一直递归下去就会把整个 Linux 都拉进可信域，所以需要**截断**。

**这次做了什么**：用 clang 导出 proxy.c、proxy_bridge.c、work.c、file.c、factory.c、syscall.c 每个函数的字段读写和数据流，再用脚本自动执行上面三步。

**结论**：

| 项目 | 数字 |
|---|---|
| oob 函数（从 6 个 oob 入口出发可达） | 23 |
| 第 1 步：oob 读的字段 | 36（其中 9 个是 Linux 对象的字段） |
| 第 2 步：往回追新增的字段 | 9 |
| 可信字段合计 | 45 |
| 范围内的函数：oob / 可信 inband / 只读门 / 不可信 | 17 / 36 / 8 / 47 |

1. **方法能自动复现我们手工做的划分。** 对 01–05 改动之前的原始 proxy.c 跑同一套分析：`relay_output`、`relay_input` 被判为可信，`kernel_write`、`kernel_read` 的返回值直接流进 `fillsz`、`reqsz`、`on_error`、`oob_wait`，Linux 的 I/O 结果进了 TCB。对现在的代码跑：它们被判为不可信，Linux 的结果在 15 个门的入口处被截断（§7）。
2. **找到了几处之前没注意到的依赖**（§8），最重要的一处是：oob 系统调用通过 `filp->f_op->oob_read` 选择要执行的 EVL 函数，**一个 Linux 能写的函数指针决定了 oob 上执行哪段代码**。
3. **截断点可以归成 9 类**，每一类都对应 doc 13/14 已有的一种处理手段（§5）。

---

## 1. 术语

| 术语 | 含义 |
|---|---|
| 可信字段 | oob 读的字段，加上第 2 步追到的字段。**以结构体类型的字段为单位**，例如 `proxy_ring.fillsz` |
| 写者 | 写可信字段的函数。包括直接赋值、原子操作，以及把字段地址传给会修改它的函数（如 `evl_flush_flag(&ring->oob_wait)`） |
| 值依赖 | 写进去的值，由哪些字段、参数、函数返回值算出来。包括控制依赖：`if (x) f = 1;` 里 `f` 依赖 `x` |
| 地址依赖 | 写的是哪个对象，由哪些数据决定。例如 `proxy->output.ring.fillsz` 中的 `proxy` 来自 `filp->private_data`。对应 doc 14 §6.4 的"写的地址必须可信" |
| 截断 | 往回追到不该、也不能拉进可信域的东西时停下，改用其他手段处理（§5） |
| 门 | 名字以 `proxy_gate_` 开头的函数。它们被约定为**会校验输入**，所以不可信的调用者把数据传进门时，在门这里截断，而不是把调用者拉进可信域 |

---

## 2. 工具

### 2.1 流程

```
gcc 的编译命令（build/linux-evl-qemu/kernel/evl/.X.o.cmd）
   │  clangargs.py：去掉 clang 不认的 gcc 选项
   ▼
clang -fsyntax-only -Xclang -ast-dump=json        每个文件约 1.3 GB 的 JSON AST
   │  extract.py
   ▼
X.facts.json      每个函数：字段读写（R/W/RW/LOCK/ADDR）、调用、数据流、返回值来源、
                  是否在 running_inband() 分支里
   │  analyze.py
   ▼
cur.txt / cur.json      第 1、2、3 步的结果，加上函数归属
```

### 2.2 extract.py 识别什么

| 代码形式 | 识别为 |
|---|---|
| `x = ring->fillsz`、`READ_ONCE(ring->x)` | 读 `proxy_ring.fillsz` |
| `ring->wroff = v`、`WRITE_ONCE(...)` | 写，值依赖 = `v` 的来源，地址依赖 = `ring` 的来源 |
| `atomic_read/set/add/sub/*_return/cmpxchg/fetch_or(&f, ...)`、`test_bit/set_bit` | 按函数语义判为读、写或读写 |
| `raw_spin_lock*(&f)`、`mutex_lock(&f)` 等 | LOCK（视为对锁的读写） |
| `*_init(&f, ...)` | 初始化写 |
| 其他函数收到 `&f` | ADDR：被调用者可能修改它，视为写 |
| 局部变量 | 函数内流不敏感地传递来源 |
| `if/while/do/for/?:` 的条件 | 作为控制依赖加到分支里的每一次写上 |
| `if (running_inband())`、`if (running_oob() \|\| ...)` 的分支 | 标为"只在 inband"，第 1 步不从这里继续找 oob 函数 |
| `container_of`、`min` 等语句表达式 | 正常展开 |

### 2.3 范围和配置

| 配置 | 取值 | 理由 |
|---|---|---|
| 分析的文件 | `proxy.c`、`proxy_bridge.c`、`work.c`、`file.c`、`factory.c`、`syscall.c`（HEAD 版本，工作区的版本第 1 行有 `++/*`），以及 `include/evl/{file,factory,work}.h` 里的内联函数 | proxy 和它直接用到的 EVL 基础设施 |
| 分析的对象类型 | `evl_proxy`、`proxy_ring`、`proxy_out`、`proxy_in`、`proxy_relay`、`proxy_bridge`、`evl_work`、`evl_file`、`evl_file_binding`、`evl_element`、`evl_factory` | 同上 |
| 固定为可信的 EVL 核心 | 其余 EVL 类型（`evl_flag`、`evl_wait_queue`、`evl_poll_head`、`evl_crossing`、线程、调度器……）和其余 EVL 函数 | 不在这次范围内，按 doc 14 §9 第 1 步"固定两端"处理 |
| oob 入口（种子） | `EVL_read`、`EVL_write`、`EVL_ioctl`（oob 系统调用），`proxy_oob_write/read/poll`（通过 `f_op` 间接调用） | 间接调用目前要手工补 |
| 门 | `proxy_gate_*` | 05 引入的门 |

### 2.4 已知的不精确

| 局限 | 影响 |
|---|---|
| **按类型区分字段，不区分实例** | 输出 ring 和输入 ring 的同名字段合在一起。原始代码里 `proxy_ring.rdoff` 既是输入方向 EVL 读者的下标，又是输出方向转发者的下标，所以转发者被当成了写可信字段的人。这其实就是 05 要拆出 `proxy_relay.off` 的原因（§7） |
| 控制依赖只看语法结构 | `if (x) return;` 之后的语句对 `x` 的依赖没有记录 |
| 跨函数传递有深度限制（返回值 4 层，参数 8 层），间接调用只记录"选择目标的字段" | 可能漏掉更深的依赖 |
| 锁宏展开后带调试条件 | `preempt_count()`、`irq_pipeline_debug_locking()` 作为控制依赖出现在锁操作上，归到"运行时底座" |
| 不分析 Linux 一侧谁写 Linux 对象的字段 | Linux 对象的字段直接作为截断点，不再往回追 |

---

## 3. 第 1 步：oob 读的字段

23 个 oob 函数：3 个 oob 系统调用、`proxy_oob_*` 3 个、它们调用的 `proxy_gate_write/read`、`can_write_buffer`、`proxy_may_*`、`proxy_nonblock`，文件和 element 引用（`evl_get_file`、`evl_get_fileref`、`evl_put_file`、`evl_get_element`、`evl_put_element`、`__evl_put_element`），以及 03 写的中继（`evl_call_inband_from`、`claim_relay`、`queue_relay`）。

它们读的 36 个字段：

**proxy 自己的对象（17 个）**

| 字段 | 写者 |
|---|---|
| `proxy_ring.bufmem`、`bufsz`、`granularity`、`wq` | `init_ring` |
| `proxy_ring.lock`、`oob_wait`、`relay_work` | `init_ring`；之后是 oob 入口、门（`out_done`、`in_done`）、`destroy_ring`、`proxy_release` |
| `proxy_ring.fillsz` | `proxy_gate_write/read`（oob）、`proxy_gate_out_consume`、`proxy_gate_in_produce` |
| `proxy_ring.wroff`、`rdoff`、`reserved`、`nesting` | 只有 `proxy_gate_write/read` |
| `proxy_in.reqsz` | oob 读者和 oob poll、门 `in_request`、`in_fulfil`、`in_stop` |
| `proxy_in.on_eof`、`on_error` | 门 `in_stop`、`in_take_eof`、oob 读者 |
| `evl_proxy.poll_head` | `proxy_factory_build`、门 `out_done`、`in_done`、oob poll |
| `evl_element.clone_flags` | `evl_init_element`、`do_element_visibility`、`evl_create_core_device` |

**文件、element 和 work（10 个）**

| 字段 | 写者 |
|---|---|
| `evl_file.filp`、`crossing`、`nonblock` | `evl_open_file`；`nonblock` 还有 02 的 `handle_inband_fflags_change`；`crossing` 还有引用计数操作 |
| `evl_file_binding.element`、`efile` | `bind_file_to_element`、`unbind_file_from_element` |
| `evl_element.ns_node`、`irq_work` | `evl_init_element`、引用计数、`__evl_put_element` |
| `evl_work.element`、`relay_flags`、`relay_node` | `evl_init_work*`、03 的中继（`claim_relay`、`queue_relay`、`run_relay_work`） |

**Linux 对象的字段（9 个）**：`file.private_data`、`file.f_mode`、`file.f_op`、`file_operations.oob_read/oob_write/oob_ioctl/compat_oob_ioctl`、`oob_file_state.data`（即 `f_oob_state.data`）、`class_fd_t.word`（fd 表查找）。它们直接成为截断点，见 §5.1。

---

## 4. 第 2 步：往回追新增的字段

| 新增字段 | 为什么被追到 | 类别 |
|---|---|---|
| `evl_proxy_attrs.bufsz`、`granularity` | `proxy_ring.bufsz`、`granularity` 的值来自它们（`proxy_factory_build` → `init_ring`） | 用户输入（uapi 结构体，`copy_from_user` 拷进来） |
| `file.f_flags` | 02 的镜像 `evl_file.nonblock` 的值来自它（`evl_open_file:65`、钩子） | Linux 字段，已经用镜像截断 |
| `inode.i_cdev` | `evl_open_element` 用 `container_of(inode->i_cdev, ...)` 找到 element，再绑定到文件（`factory.c:192`） | Linux 字段，**新发现**（§8.2） |
| `task_struct.mm` | `do_element_visibility` 根据 `current->mm` 设置 `clone_flags` | 运行时底座（`current`） |
| `evl_element.fpriv.filp` | `create_element_device` 等用它决定 element 的引用操作 | EVL，写者是 factory |
| `evl_element.scope` | element 的引用计数操作依赖它 | EVL |
| `evl_factory.build` | `ioctl_clone_device` 通过函数指针 `fac->build` 创建 element，返回值存进 binding | EVL，函数指针 |
| `llist_node.next` | 03 的 `relay_interrupt` 遍历链表节点，节点决定要写哪个 `evl_work` | 通用容器字段：类型是 Linux 的，实例是 EVL 的 per-CPU 中继链表 |

第 2 步只增加了 9 个字段就停了，因为往回追很快就碰到截断点：用户输入、Linux 字段、分配器、门。

---

## 5. 第 3 步：截断点

按类别列出，每一类都对应一种已有的处理手段。

| # | 类别 | 例子 | 处理 |
|---|---|---|---|
| 5.1 | **Linux 对象的字段** | `file.private_data`、`file.f_op`、`f_oob_state.data`、`file.f_mode`、`inode.i_cdev`、fd 表 | 打开文件时存快照，以后从 EVL 自己的 `evl_file` 读（doc 13 §11.3）；fd 表和引用计数进共享区（doc 13 §11.5） |
| 5.2 | **门的输入来自不可信调用者**（18 处） | `relay_output → proxy_gate_out_consume → fillsz`；`relay_input → proxy_gate_in_produce → fillsz`；`proxy_bridge_write → proxy_gate_write` | 门里校验。05 第一阶段只报警，这 18 处就是校验要覆盖的全部入口 |
| 5.3 | **用户输入** | 系统调用参数 `count`、`u_buf` 的内容（经 `raw_copy_from_user`，作为控制依赖：拷贝失败会走另一条路径）、`evl_proxy_attrs`、`clone_flags` | 已经有校验：`count > bufsz` 返回 `-EFBIG`、粒度检查、`order_base_2(bufsz) > 30` 等。要确认覆盖完整 |
| 5.4 | **分配器的返回值**（16 处） | `kzalloc` → `bufmem`、`evl_proxy` 本体；`kmalloc` → `evl_file_binding`；`alloc_workqueue` → `wq` | 这是**地址依赖**：可信对象放在哪里由 Linux 分配器决定。隔离时要换成 EVL 自己的堆或 LKL 的内存池（2026-11-06 讨论的结论） |
| 5.5 | **Linux 函数的返回值** | `anon_inode_getfile` → `evl_file.filp`；`queue_work`；`llist_del_all`；`fd_empty` | 按函数分别处理：`anon_inode_getfile` 返回 Linux 对象，属于 5.1；`llist_*` 是无状态的链表操作，可以交给 LKL 或者自己实现；`queue_work` 只在 inband 上调用 |
| 5.6 | **Linux 函数直接操作可信对象** | `irq_work_queue(&e->irq_work)`（`__evl_put_element:288`，**在 oob 上**）；`llist_add(&work->relay_node)`（`queue_relay:122`） | `irq_work_queue` 和 03 改掉的是同一类问题，换成 03 的中继即可（03 §8 已列为待办）；`llist_add` 是内联的无状态函数 |
| 5.7 | **运行时底座** | `preempt_count()`、`hard_irqs_disabled()`、`current_work()`、`dovetail_mm_state()` | 共享区（doc 13 §11.5），写成信任假设 |
| 5.8 | **入口参数** | 通过函数指针被调用的函数的参数：`proxy_oob_write(count)`、`proxy_factory_build(clone_flags)`、`evl_open_element(filp)` | 来自系统调用的参数属于用户输入（5.3）；VFS 传进来的 `filp` 属于 Linux 对象（5.1） |
| 5.9 | **EVL 核心**（固定为可信） | `evl_wait_flag` 的返回值、`evl_poll_head` 等对象 | 不在这次范围内 |

---

## 6. 函数归属

归属规则（doc 14 §8.1 加上本次流程）：

- **A oob**：从 oob 入口可达；
- **B 可信，inband**：不是 oob，但写了可信字段，或者把数据传给了一个**不是门**的可信写者；
- **C 只读门**：只读可信字段，不写；
- **D 不可信**：不读写可信字段（经门除外）。

proxy 的两个文件：

| 类别 | 函数 |
|---|---|
| A oob | `proxy_oob_write`、`proxy_oob_read`、`proxy_oob_poll`、`proxy_gate_write`、`proxy_gate_read`、`can_write_buffer`、`proxy_may_read`、`proxy_may_write`、`proxy_nonblock` |
| B 可信，inband | `init_ring`、`destroy_ring`、`proxy_factory_build`、`proxy_factory_dispose`、`proxy_release`；门 `proxy_gate_out_consume`、`out_done`、`in_produce`、`in_fulfil`、`in_request`、`in_stop`、`in_done`、`in_take_eof` |
| C 只读门 | `proxy_gate_bridge`、`proxy_gate_can_write`、`proxy_gate_out_fill`、`proxy_gate_in_fill`、`proxy_gate_in_requested` |
| D 不可信 | `relay_output`、`relay_input`、`proxy_bridge_write/read/poll/mmap`、`proxy_bridge_relay_output/input`、`proxy_bridge_create/destroy`、`init_relay`，以及 **`relay_output_work`、`relay_input_work`** |

和 05 的手工划分对照：

- 完全一致的部分：转发者和 inband 文件操作是不可信的；门、生命周期函数、`proxy_release` 是可信的。
- **不一致的一处**：`relay_output_work` / `relay_input_work` 被判为不可信。它们只读 `evl_proxy.bridge`（不是可信字段）然后调用 bridge，所以按规则可以挪进 `proxy_bridge.c`。doc 14 §10.1 把它们写成"可信，inband"，是我当时按"放在 proxy.c 里"来判断的，应该以这次结果为准。

其余文件的结果（详见 `out/cur.txt` 的"函数归属"一节）：

| 文件 | B 可信，inband | 说明 |
|---|---|---|
| `factory.c` | `evl_init_element`、`evl_init_user_element`、`bind_file_to_element`、`unbind_file_from_element`、`evl_open_element`、`evl_release_element`、`do_element_visibility`、`create_element_device`、`evl_create_core_device`、`ioctl_clone_device`、`release_clone_device` | element 的创建、打开、绑定、释放都是可信 inband 代码，和 doc 14 §3 的推导一致 |
| `file.c` | `evl_open_file`、`evl_release_file`、`handle_inband_fflags_change` | 02 的钩子是可信代码：它写 `evl_file.nonblock` |
| `work.c` | `evl_init_work*`、`run_relay_work`、`do_relay_work`、`relay_interrupt`、`__evl_do_work`、`evl_call_inband_sync_from` | 03 写的中继，它在 inband 上的那一半也是可信代码 |

---

## 7. 对照：原始 proxy.c 和现在

同一套工具，对 01–05 改动之前的 `proxy.c`（`git show HEAD:kernel/evl/proxy.c`）再跑一次，其余文件不变：

| 项目 | 原始 | 现在 | 对应的修改 |
|---|---|---|---|
| `relay_output`、`relay_input` | **B 可信**：直接写 `fillsz`、`reqsz`、`on_error`，直接唤醒 `oob_wait` | **D 不可信** | 05 |
| `proxy_write`、`proxy_read`（inband 入口） | **B 可信** | **D 不可信**（`proxy_bridge_write/read`） | 05 |
| `kernel_write()` / `kernel_read()` 的返回值 | 作为截断点出现 16 次，流进 `fillsz`、`reqsz`、`on_error`、`on_eof`、`oob_wait`、`poll_head` | 不再出现，换成 18 处"门的输入" | 05 |
| `prepare_to_wait_event()` | 作为截断点出现：inband 写者的 `wait_event` 条件直接读可信字段 | 不再出现：条件改由门 `proxy_gate_can_write` 计算 | 05 |
| `evl_proxy.filp`、`file_operations.oob_poll` | 是可信字段：`proxy_oob_poll` 在 oob 上读目标文件的 `f_op->oob_poll` | 不再是 | 01 |
| `file.f_flags` | oob 直接读（第 1 步） | 只作为镜像的来源出现（第 2 步） | 02 |
| `proxy_ring.rdoff` 的写者 | `do_proxy_read`（EVL 读者）**和** `relay_output`（转发者），因为两个方向共用一个字段 | 只有 `proxy_gate_read` | 05 拆出 `proxy_relay.off` |
| "Linux 函数的返回值"类截断点 | 35 | 12 | — |

所以这套方法**不需要人工判断**，就把原始代码里"Linux I/O 的结果直接写进可信状态"的地方全部标了出来，而我们手工做的 01、02、05 正好消掉了这些地方。这也说明 05 的拆分不是凭感觉做的，按"oob 依赖"来定义可信字段，自然就会得到这个结果。

---

## 8. 新发现

都是工具的输出加上读代码得出的，**没有运行验证**。

### 8.1 Linux 能写的函数指针决定 oob 执行哪段代码

`EVL_read`（`syscall.c:44`）在 oob 上这样分派：

```c
struct evl_file *efilp = evl_get_file(fd);     // fd 表 → file → f_oob_state.data
filp = efilp->filp;
...
ret = filp->f_op->oob_read(filp, u_buf, size); // 函数指针放在 Linux 的 struct file 里
```

`file.f_op` 和 `file_operations.oob_read` 都在可信字段里，而它们放在 Linux 对象里。这不只是数据完整性的问题，**它直接决定 oob 上的控制流**，也就是可信代码的完整性。

处理：打开文件时，把 `f_op` 存进 `evl_file` 作为快照（EVL 的 fops 表本身是 EVL 的静态常量），oob 分派从 `efilp` 里取。`f_op` 在 open 之后基本不变，属于 doc 13 §11.3 的第一类。另外，EVL 自己也会改 `file.f_op`：私有 element 创建时先换成 `dummy_fops`，再换回真正的 fops（`do_element_visibility:419,438`）。快照必须在换回之后再取。

### 8.2 打开哪个 element，由 Linux 的 cdev 决定

```c
int evl_open_element(struct inode *inode, struct file *filp)
{
	e = container_of(inode->i_cdev, struct evl_element, cdev);   // factory.c:192
	...
	ret = bind_file_to_element(filp, e);
```

公开 element 通过 `/dev/evl/...` 打开时，文件绑定到哪个 element，是 Linux 根据设备号查 cdev 表得出的 `inode->i_cdev`。之后 oob 上所有的 `element_of(filp)`，用的都是这次绑定的结果。

处理：`bind_file_to_element` 收到的 `e` 要校验，确认它确实是 EVL 自己登记过的 element，比如在 factory 的表里查一次。这是 doc 14 §10.2 #3（`element_of(filp)` 用了不可信的控制信息）在 open 一侧的根源。

### 8.3 从 fd 到 element 的整条链都经过 Linux 对象

oob 入口找到 proxy 对象的路径是：

```
fd ──(Linux fd 表)──▶ struct file ──f_oob_state.data──▶ evl_file
                       struct file ──private_data──▶ evl_file_binding ──element──▶ evl_proxy
```

其中三个指针放在 Linux 的对象里：fd 表、`f_oob_state.data`、`private_data`。工具把这三个都列成了可信字段。

doc 14 §10.2 #3 只指出了 `private_data` 一个。实际上要改的是整条链：

- fd 表属于共享区（doc 13 §11.5）；
- 有了 `evl_file` 之后，`private_data` 这一跳可以省掉：`evl_file_binding` 本来就包含 `evl_file`，从 `efilp` 用 `container_of` 就能拿到 binding；
- `f_oob_state.data` 是 Dovetail 的字段，进共享区，读的时候校验（比如核对 `efilp->filp == filp`）。

### 8.4 oob 上仍然有一处调用 Linux 的 irq_work

`__evl_put_element`（`factory.c:286-288`）在 oob 上，或者硬件中断关闭时，用 Linux 的 `irq_work_queue` 推迟释放 element。这和 03 处理的是同一类问题，03 只改了 `evl_work` 这一条路径。

### 8.5 分配器决定可信对象放在哪里

16 处地址依赖来自 `kzalloc`、`kmalloc`、`alloc_workqueue`：`evl_proxy` 本体、`bufmem`、`evl_file_binding` 都是 Linux 分配的。这从工具层面证实了 2026-11-06 讨论的结论：分配器出错，可信数据的完整性就保证不了。

### 8.6 两个 work 回调可以挪进 bridge

见 §6。

---

## 9. 下一步

1. **人工核实** `out/cur.txt` 里的 45 个字段和 9 类截断点，尤其是 §8 的几条。
2. **扩大范围**：把 `wait.c`、`poll.c`、`sched/`、`thread.c` 纳入分析，不再把它们固定为可信。这会显著增加可信字段和截断点。
3. **补齐间接调用**：fops 表、work 的 handler、factory 的 `build`/`dispose`，目前是手工补的种子，可以从 `file_operations` 的初始化列表里自动提取。
4. **提高精度**：按访问路径（`evl_proxy.output.ring.rdoff`）区分实例；补上早返回带来的控制依赖。
5. **处理 §8 的发现**：`f_op` 快照、`bind_file_to_element` 校验、从 fd 到 element 的链、`__evl_put_element` 改用 03 的中继、两个回调挪进 bridge。
