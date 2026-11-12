#!/usr/bin/env python3
# Generate docs/figs/irq-work-fields.svg: functions (colored by owner),
# every struct field, and which function reads/writes which field.
import sys
from xml.sax.saxutils import escape

OUT = sys.argv[1]

OWN = {  # fill, stroke, label
    'E': ('#E3EEF7', '#0072B2', 'EVL'),
    'L': ('#E1F3EC', '#009E73', 'Linux'),
    'D': ('#FCEFD7', '#E69F00', 'Dovetail'),
}

# ---------------------------------------------------------------- structs
# (key, owner, title, where, note, [(id, name, type, meaning)], column)
STRUCTS = [
 ('X', 'E', 'struct evl_proxy', 'proxy.c:56', '一个 proxy：把 oob 读写转发到普通文件', [
  ('X1', 'filp', 'struct file *', '目标文件，数据最终写到这里'),
  ('X2', 'output', 'struct proxy_out', '输出方向，内含一个 proxy_ring'),
  ('X3', 'input', 'struct proxy_in', '输入方向：ring + reqsz/on_eof/on_error'),
  ('X4', 'element', 'struct evl_element', 'EVL 元素公共部分'),
  ('X5', 'poll_head', 'struct evl_poll_head', 'oob poll() 的等待者'),
 ], 1),
 ('E', 'E', 'struct evl_element', 'factory.h:66', '所有 EVL 对象（线程、proxy…）的公共部分', [
  ('E1', 'rcu', 'struct rcu_head', 'RCU 延迟释放'),
  ('E2', 'factory', 'struct evl_factory *', '所属类型的工厂'),
  ('E3', 'cdev', 'struct cdev', '字符设备'),
  ('E4', 'dev', 'struct device *', '/dev/evl/... 设备'),
  ('E5', 'devname', 'struct filename *', '设备名'),
  ('E6', 'minor', 'unsigned int', '次设备号'),
  ('E7', 'clone_flags', 'int', '创建标志 PUBLIC/OUTPUT/INPUT…'),
  ('E8', 'irq_work', 'struct irq_work', '销毁元素时用（本路径不用）'),
  ('E9', 'flush', 'struct list_head', '待销毁链表节点'),
  ('E10', 'hash', 'struct hlist_node', '按名字查找的哈希节点'),
  ('E11', 'fpriv', '{filp, efd}', '元素自己的文件和 fd'),
  ('E12', 'scope', 'struct evl_scope *', '所属命名空间'),
  ('E13', 'ns_node', 'struct evl_map_node', '{fundle, refs, rb_node}，refs=引用计数'),
  ('E14', 'owned', 'struct list_head', '挂在所属进程的元素链表上'),
 ], 1),
 ('P', 'E', 'struct proxy_ring', 'proxy.c:28', '一个方向的环形缓冲和配套设施', [
  ('P1', 'bufmem', 'void *', '缓冲区内存'),
  ('P2', 'fillsz', 'atomic_t', '已提交、可被读走的字节数'),
  ('P3', 'nesting', 'int', '正在写的写者个数'),
  ('P4', 'bufsz', 'unsigned int', '缓冲区大小'),
  ('P5', 'rdoff', 'unsigned int', '读位置（inband 转发者）'),
  ('P6', 'wroff', 'unsigned int', '写位置，下一次预留从这里开始'),
  ('P7', 'reserved', 'unsigned int', '已预留、还在拷贝的字节数'),
  ('P8', 'granularity', 'unsigned int', '写入粒度，每次须为其整数倍'),
  ('P9', 'oob_wait', 'struct evl_flag', 'oob 读写者等缓冲时睡在这里'),
  ('P10', 'inband_wait_r', 'wait_queue_head_t', 'inband 读者等待队列'),
  ('P11', 'inband_wait_w', 'wait_queue_head_t', 'inband 写者等待队列'),
  ('P12', 'relay_work', 'struct evl_work', '叫 inband 来转发的"包裹"'),
  ('P13', 'lock', 'hard_spinlock_t', '保护缓冲指针的硬锁，oob 可用'),
  ('P14', 'wq', 'struct workqueue_struct *', '专用 ordered 队列'),
  ('P15', 'worker_lock', 'struct mutex', '串行化 inband 转发'),
 ], 1),
 ('WQ', 'L', 'struct workqueue_struct', 'workqueue.c', '', [], 1),
 ('W', 'E', 'struct evl_work', 'work.h:16', '跨 stage 推迟执行的"包裹"', [
  ('W1', 'irq_work', 'struct irq_work', '第一段：oob → inband 中断'),
  ('W2', 'wq_work', 'struct work_struct', '第二段：中断 → kworker'),
  ('W3', 'wq', 'struct workqueue_struct *', '第二段投到哪个队列'),
  ('W4', 'handler', '函数指针（联合体）', '最终执行 = relay_output_work'),
  ('W5', 'element', 'struct evl_element *', '被保护的元素，入队前加引用'),
 ], 2),
 ('I', 'L', 'struct irq_work', 'irq_work.h', '一个要在硬中断里执行的函数', [
  ('I1', 'node', 'struct __call_single_node', '链表节点 + 标志位'),
  ('I2', 'func', 'void (*)(struct irq_work *)', '回调 = __evl_do_irq_work'),
  ('I3', 'irqwait', 'struct rcuwait', 'irq_work_sync 在此等回调结束'),
 ], 2),
 ('N', 'L', 'struct __call_single_node', 'smp_types.h:58', 'irq_work 与 smp_call_function 共用的节点', [
  ('N1', 'llist.next', 'struct llist_node *', '指向链表里的下一个节点'),
  ('N2', 'a_flags', 'atomic_t（=u_flags）', 'PENDING/BUSY/LAZY/HARD_IRQ + 类型'),
  ('N3', 'src', 'u16', '调试：跨 CPU 调用的来源'),
  ('N4', 'dst', 'u16', '调试：跨 CPU 调用的目标'),
 ], 2),
 ('Q', 'L', 'per-CPU struct llist_head ×2', 'irq_work.c:27', '每个 CPU 两条 irq_work 链表', [
  ('Q1', 'raised_list.first', 'struct llist_node *', '要按门铃的链表（信箱①）'),
  ('Q2', 'lazy_list.first', 'struct llist_node *', '等 tick 执行的链表表头'),
 ], 2),
 ('K', 'L', 'struct work_struct', 'workqueue_types.h', '一个要在进程上下文执行的函数', [
  ('K1', 'data', 'atomic_long_t', 'PENDING 位 + 所属 pool_workqueue'),
  ('K2', 'entry', 'struct list_head', '挂进 worker pool 的链表节点'),
  ('K3', 'func', 'work_func_t', '回调 = __evl_do_work'),
  ('K4', 'lockdep_map', 'struct lockdep_map', '仅 CONFIG_LOCKDEP：死锁检测'),
 ], 2),
 ('D', 'D', 'per-CPU struct irq_pipeline_data', 'irqstage.h:45', '每个 CPU 一份的流水线总结构', [
  ('D1', 'stages[2]', 'struct irq_stage_data', '[0]=inband，[1]=oob'),
  ('D2', 'tick_regs', 'struct pt_regs', 'tick 中断时的寄存器现场'),
  ('D3', 'task_inflight', 'struct task_struct *', '正在切换 stage 的任务'),
  ('D4', 'rqlock_owner', 'struct task_struct *', '持有运行队列锁的任务'),
  ('D5', 'vcpu_notify', 'struct kvm_oob_notifier *', '仅 CONFIG_KVM'),
 ], 2),
 ('S', 'D', 'struct irq_stage_data', 'irqstage.h', '某个 CPU 上某个 stage 的数据', [
  ('S1', 'log', 'struct irq_log', '中断日志（信箱②）'),
  ('S2', 'stage', 'struct irq_stage *', '属于哪个 stage'),
  ('S3', 'cpu', 'int', '仅调试配置：所属 CPU'),
 ], 2),
 ('G', 'D', 'struct irq_log + irq_event_map', 'pipeline.c:68', '多级位图，每个中断号一位', [
  ('G1', 'index_0', 'unsigned long', '顶层索引：哪一大段有位被设上'),
  ('G2', 'map', 'struct irq_event_map *', '指向下层位图'),
  ('G3', 'map->index_1[]', 'unsigned long[]', '中间层索引（≥3 级时）'),
  ('G4', 'map->index_2[]', 'unsigned long[]', '中间层索引（4 级时）'),
  ('G5', 'map->flat[]', 'unsigned long[]', '每个中断号一位：待处理'),
 ], 2),
 ('T', 'D', 'struct irq_stage', 'irqstage.h', '全局只有两个：inband_stage / oob_stage', [
  ('T1', 'index', 'int', '0=inband，1=oob，用来索引 stages[]'),
  ('T2', 'name', 'const char *', '名字'),
 ], 2),
 ('C', 'D', '其他状态', '', 'stage / 中断开关', [
  ('C1', 'current->stall_bits', 'unsigned long', 'INBAND/OOB_STALL_BIT：虚拟关中断'),
  ('C2', 'preempt_count STAGE 位', 'per-CPU', '当前在哪个 stage（running_inband）'),
  ('C3', 'CPU 的 IF 标志', '硬件', '真正的硬中断开关'),
 ], 2),
 ('A', 'D', 'struct irqaction inband_irq_work', 'pipeline.c:1709', 'inband_work_sirq 上绑定的动作（其余字段默认）', [
  ('A1', 'handler', 'irq_handler_t', '= inband_work_interrupt'),
  ('A2', 'name', 'const char *', '"in-band irq work"'),
  ('A3', 'flags', 'unsigned int', 'IRQF_NO_THREAD：必须在硬中断执行'),
 ], 2),
]

# ---------------------------------------------------------------- functions
# phase: (title, explanation)
PHASES = [
 ('初始化 · inband（创建 proxy 时）', '包裹和队列都事先准备好，oob 上就不用分配内存'),
 ('第一段 · oob 线程', '同步完成：不拿 Linux 锁、不睡眠、不等待；碰到的非 EVL 状态只有 N1 N2 Q1（Linux）和 G*（Dovetail）'),
 ('第二段 · inband 中断上下文', 'oob 让出 CPU、inband 恢复后，Dovetail 回放日志；可以拿自旋锁，不能睡眠'),
 ('第三段 · kworker 进程上下文', '可以睡眠、走 VFS'),
]
# (step label, phase, owner, name, where, desc, reads, writes, rw, note)
FUNCS = [
 ('i1', 0, 'E', 'init_ring', 'proxy.c:624', '分配缓冲，创建队列，初始化锁、等待队列和包裹',
  [], ['P1', 'P4', 'P8', 'P9', 'P10', 'P11', 'P13', 'P14', 'P15'], [], ''),
 ('i2', 0, 'L', 'alloc_ordered_workqueue', 'workqueue.h:566', '创建 unbound + ordered（max_active=1）队列，返回值存进 P14',
  [], [], [], '创建 workqueue_struct'),
 ('i3', 0, 'E', 'evl_init_work_safe', 'work.c:66', '经 evl_init_work 填好包裹；wq 此时不填',
  [], ['W4', 'W5'], [], ''),
 ('i4', 0, 'L', 'init_irq_work', 'irq_work.h', '清零 irq_work，设回调',
  [], ['N1', 'N2', 'I2', 'I3'], [], ''),
 ('i5', 0, 'L', 'INIT_WORK', 'workqueue.h', '初始化 work_struct，设回调',
  [], ['K1', 'K2', 'K3'], [], ''),

 ('1', 1, 'E', 'proxy_oob_write', 'proxy.c:398', 'oob_write() 的 proxy 实现；缓冲满时在 oob_wait 上睡',
  ['E7'], [], ['P9'], ''),
 ('2', 1, 'E', 'do_proxy_write', 'proxy.c:159', '拿硬锁→预留→拷数据→最后一个写者提交；空变非空时通知 inband',
  ['P4', 'P8', 'P12', 'P14'], ['P1'], ['P2', 'P3', 'P6', 'P7', 'P13'], ''),
 ('3', 1, 'E', 'evl_call_inband_from', 'work.c:85', '写下第二段目的地，加引用，交给 irq_work',
  ['W5'], ['W3'], [], ''),
 ('4', 1, 'E', 'evl_get_element', 'factory.h:122', '元素引用 +1（refcount_inc_not_zero）',
  [], [], ['E13'], ''),
 ('5', 1, 'L', 'irq_work_queue', 'irq_work.c:116', 'irq_work 入队 API：依次调用 6、7、8；preempt_disable 防迁移',
  [], [], [], '自身不碰字段'),
 ('6', 1, 'L', 'irq_work_claim', 'irq_work.c:57', 'atomic_fetch_or 设 PENDING|BUSY；原来就 PENDING 则返回 false',
  [], [], ['N2'], ''),
 ('7', 1, 'L', '__irq_work_queue_local', 'irq_work.c:85', '按 LAZY 选链表；llist_add 用 cmpxchg 换表头；原来为空才按门铃',
  ['N2'], ['N1'], ['Q1'], ''),
 ('8', 1, 'D', 'irq_local_work_raise', 'pipeline.c:1715', '强符号替换原版（原版发自 IPI）：硬关中断，post sirq',
  ['C1', 'C2'], [], ['C3'], ''),
 ('9', 1, 'D', 'irq_post_stage', 'pipeline.c:613', '经 irq_post_inband 调用：在本 CPU inband 日志里设上 inband_work_sirq 的位',
  ['T1', 'D1'], ['G1', 'G3', 'G5'], [], 'G4 仅 4 级时'),

 ('10', 2, 'D', 'sync_current_irq_stage', 'pipeline.c:1322', '回放日志；从中断退出路径或 inband 解除 stall 时进入',
  ['C2', 'D1'], [], ['C1', 'C3'], ''),
 ('11', 2, 'D', 'pull_next_irq', 'pipeline.c（内联）', '用 __ffs 逐级找最低的待处理中断号，清掉它的位',
  ['G2'], [], ['G1', 'G3', 'G5'], ''),
 ('12', 2, 'D', 'do_inband_irq', 'pipeline.c:979', '开硬中断（inband 仍 stall），经 irq_desc 调 irqaction 的 handler',
  ['A1'], [], ['C3'], ''),
 ('13', 2, 'D', 'inband_work_interrupt', 'pipeline.c:1702', 'sirq 的处理函数，只调 irq_work_run',
  [], [], [], '自身不碰字段'),
 ('14', 2, 'L', 'irq_work_run', 'irq_work.c:274', '对本 CPU 的两条链表各调一次 15',
  [], [], [], '只取链表地址'),
 ('15', 2, 'L', 'irq_work_run_list', 'irq_work.c:256', 'llist_del_all（xchg 置 NULL）摘下整条链，逐个调 16',
  ['C1', 'N1'], [], ['Q1', 'Q2'], ''),
 ('16', 2, 'L', 'irq_work_single', 'irq_work.c:203', '清 PENDING → 调 func → cmpxchg 清 BUSY',
  ['I2'], [], ['N2'], ''),
 ('17', 2, 'E', '__evl_do_irq_work', 'work.c:37', 'container_of 找回包裹，投进第二段队列；失败则退回引用',
  ['W3', 'W5'], [], [], ''),
 ('18', 2, 'L', 'queue_work', 'workqueue.c:2415', 'test_and_set 设 PENDING，挂进 pool 链表，唤醒 kworker',
  [], ['K2'], ['K1'], '读 workqueue_struct'),

 ('19', 3, 'L', 'process_one_work', 'workqueue.c:3163', 'kworker 取出 work：摘链表、清 PENDING、调 func',
  ['K3'], [], ['K1', 'K2'], ''),
 ('20', 3, 'E', '__evl_do_work', 'work.c:9', '调 handler，返回后退回引用（调 24）',
  ['W4', 'W5'], [], [], ''),
 ('21', 3, 'E', 'relay_output_work', 'proxy.c:145', 'container_of 从包裹找回 proxy，调 22',
  [], [], [], '只做指针换算'),
 ('22', 3, 'E', 'relay_output', 'proxy.c:75', '拿 worker_lock，把缓冲写进目标文件；唤醒 oob 写者和 inband 写者',
  ['X1', 'P1', 'P4', 'P8', 'X5'], ['P9', 'P11'], ['P2', 'P5', 'P15'], ''),
 ('23', 3, 'L', 'kernel_write', 'VFS', '把内核缓冲写进文件，可能睡眠',
  ['P1', 'X1'], [], [], ''),
 ('24', 3, 'E', 'evl_put_element', 'factory.h:169', '元素引用 −1，到零则销毁元素',
  [], [], ['E13'], ''),
]

FIELDNAME = {}
for key, own, title, where, note, fields, col in STRUCTS:
    for fid, name, typ, mean in fields:
        FIELDNAME[fid] = name

ACCESS = {}  # fid -> [(step, mode)]
for step, ph, own, name, where, desc, r, w, rw, note in FUNCS:
    for mode, lst in (('W', w), ('RW', rw), ('R', r)):
        for f in lst:
            assert f in FIELDNAME, f
            ACCESS.setdefault(f, []).append((step, mode))

# ---------------------------------------------------------------- drawing
o = []
def add(s): o.append(s)
def t(x, y, s, cls, extra=''):
    add(f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}"{extra}>{escape(s)}</text>')

def wcn(s, size):  # rough text width: CJK ~ size, ascii ~ 0.56*size
    w = 0
    for ch in s:
        w += size if ord(ch) > 0x2e80 else size * 0.56
    return w
def wmono(s, size):
    w = 0
    for ch in s:
        w += size if ord(ch) > 0x2e80 else size * 0.61
    return w

MODE_STYLE = {
    'W': ('#333333', '#333333', '#FFFFFF'),
    'R': ('#FFFFFF', '#333333', '#000000'),
    'RW': ('#F2C14E', '#333333', '#000000'),
}

W_TOTAL = 1960
LEFT_X, LEFT_W = 20, 740
COL1_X, COL2_X, COLW = 790, 1380, 556

# ---- left column: functions
y = 150
FUNC_POS = {}
for pi, (ptitle, pdesc) in enumerate(PHASES):
    add(f'<rect x="{LEFT_X}" y="{y}" width="{LEFT_W}" height="34" fill="#F4F4F4" stroke="#999" stroke-width="0.8"/>')
    t(LEFT_X + 10, y + 15, ptitle, 'ph')
    t(LEFT_X + 10, y + 29, pdesc, 'cap')
    y += 42
    for f in [f for f in FUNCS if f[1] == pi]:
        step, ph, own, name, where, desc, r, w, rw, note = f
        fill, stroke, olab = OWN[own]
        # chips layout
        chips = [(x, 'W') for x in w] + [(x, 'RW') for x in rw] + [(x, 'R') for x in r]
        cx0 = LEFT_X + 44
        cx = cx0
        cy = y + 38
        lines = 1
        chip_items = []
        for fid, mode in chips:
            label = f'{fid} {FIELDNAME[fid]}'
            cw = wmono(label, 9.5) + 10
            if cx + cw > LEFT_X + LEFT_W - 8:
                cx = cx0; cy += 18; lines += 1
            chip_items.append((cx, cy, cw, label, mode))
            cx += cw + 5
        if note:
            nw = wcn(note, 10.5)
            if cx + nw > LEFT_X + LEFT_W - 8 and chips:
                cx = cx0; cy += 18; lines += 1
            chip_items.append((cx, cy, nw, note, 'note'))
        h = 30 + 18 * lines + 2
        add(f'<rect x="{LEFT_X}" y="{y}" width="{LEFT_W}" height="{h}" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>')
        add(f'<rect x="{LEFT_X}" y="{y}" width="6" height="{h}" fill="{stroke}"/>')
        # step badge
        bw = 26 if len(step) > 1 else 20
        add(f'<rect x="{LEFT_X + 12}" y="{y + 7}" width="{bw}" height="16" rx="8" fill="#000"/>')
        t(LEFT_X + 12 + bw / 2, y + 19, step, 'snt')
        t(LEFT_X + 44, y + 19, name, 'fnm')
        nx = LEFT_X + 44 + wmono(name, 11.5) + 8
        add(f'<rect x="{nx:.1f}" y="{y + 8}" width="{wcn(olab, 9.5) + 10:.1f}" height="14" rx="2" fill="{stroke}"/>')
        t(nx + 5, y + 19, olab, 'otag')
        nx += wcn(olab, 9.5) + 16
        t(nx, y + 19, where, 'loc')
        nx += wmono(where, 9.5) + 10
        t(nx, y + 19, desc, 'dsc')
        for (x0, y0, cw, label, mode) in chip_items:
            if mode == 'note':
                t(x0, y0 + 2, label, 'cap')
                continue
            bg, bd, fg = MODE_STYLE[mode]
            add(f'<rect x="{x0:.1f}" y="{y0 - 10}" width="{cw:.1f}" height="15" rx="2" fill="{bg}" stroke="{bd}" stroke-width="1"/>')
            add(f'<text class="chip" x="{x0 + 5:.1f}" y="{y0 + 1}" fill="{fg}" style="fill:{fg}">{escape(label)}</text>')
        FUNC_POS[step] = (y, h)
        y += h + 6
    y += 6
LEFT_BOTTOM = y

# ---- right columns: structs
ROWH = 17
colY = {1: 150, 2: 150}
HDR = {}
ROWPOS = {}
for key, own, title, where, note, fields, col in STRUCTS:
    fill, stroke, olab = OWN[own]
    x = COL1_X if col == 1 else COL2_X
    y0 = colY[col]
    if key == 'WQ':
        h = 44
        add(f'<rect x="{x}" y="{y0}" width="{COLW}" height="{h}" rx="3" fill="#fff" stroke="{stroke}" stroke-width="1.2" stroke-dasharray="5 3"/>')
        add(f'<rect x="{x}" y="{y0}" width="{COLW}" height="20" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>')
        t(x + 8, y0 + 14, title, 'sth')
        t(x + 8 + wmono(title, 11) + 10, y0 + 14, f'{olab} · {where}', 'loc')
        t(x + 8, y0 + 37, 'Linux 内部结构，字段很多；本路径只通过 alloc/queue_work 等 API 使用，按不透明对象处理', 'cap')
        HDR[key] = (x, y0)
        colY[col] = y0 + h + 22
        continue
    h = 36 + ROWH * len(fields) + 4
    add(f'<rect x="{x}" y="{y0}" width="{COLW}" height="{h}" rx="3" fill="#fff" stroke="{stroke}" stroke-width="1.2"/>')
    add(f'<rect x="{x}" y="{y0}" width="{COLW}" height="34" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.2"/>')
    t(x + 8, y0 + 14, title, 'sth')
    t(x + 8 + wmono(title, 11) + 10, y0 + 14, f'{olab}' + (f' · {where}' if where else ''), 'loc')
    t(x + 8, y0 + 28, note, 'cap')
    HDR[key] = (x, y0)
    ry = y0 + 36
    for i, (fid, name, typ, mean) in enumerate(fields):
        if i % 2 == 1:
            add(f'<rect x="{x + 1}" y="{ry}" width="{COLW - 2}" height="{ROWH}" fill="#F7F7F7"/>')
        add(f'<rect x="{x + 5}" y="{ry + 3}" width="28" height="11" rx="2" fill="{stroke}"/>')
        t(x + 19, ry + 12, fid, 'fid')
        t(x + 38, ry + 12.5, name, 'fname')
        t(x + 38 + wmono(name, 10) + 6, ry + 12.5, typ, 'ftype')
        mxs = max(250, 38 + wmono(name, 10) + 6 + wmono(typ, 9) + 10)
        t(x + mxs, ry + 12.5, mean, 'fmean')
        # access markers, right aligned
        acc = ACCESS.get(fid, [])
        mx = x + COLW - 6
        for step, mode in reversed(acc):
            bg, bd, fg = MODE_STYLE[mode]
            mw = 9 + 6 * len(step)
            mx -= mw
            add(f'<rect x="{mx:.1f}" y="{ry + 2.5}" width="{mw}" height="12" rx="2" fill="{bg}" stroke="{bd}" stroke-width="0.8"/>')
            add(f'<text class="mk" x="{mx + mw / 2:.1f}" y="{ry + 11.5}" style="fill:{fg}">{escape(step)}</text>')
            mx -= 3
        ROWPOS[fid] = (x, ry)
        ry += ROWH
    colY[col] = y0 + h + 22

# ---- embedding arrows between structs
def emb(fid, key, side, gx, label=''):
    x, ry = ROWPOS[fid]
    hx, hy = HDR[key]
    sy = ry + ROWH / 2
    ty = hy + 10
    if side == 'L':
        sx = x
        add(f'<path class="emb" d="M{sx},{sy} L{gx},{sy} L{gx},{ty} L{hx},{ty}"/>')
    elif side == 'R':
        sx = x + COLW
        add(f'<path class="emb" d="M{sx},{sy} L{gx},{sy} L{gx},{ty} L{hx + COLW},{ty}"/>')
    else:  # cross from col1 right edge to col2 left edge
        sx = x + COLW
        add(f'<path class="emb" d="M{sx},{sy} L{gx},{sy} L{gx},{ty} L{hx},{ty}"/>')

emb('X2', 'P', 'L', COL1_X - 14)
emb('X4', 'E', 'L', COL1_X - 22)
emb('P14', 'WQ', 'L', COL1_X - 14)
emb('P12', 'W', 'X', COL1_X + COLW + 17)
emb('W1', 'I', 'R', COL2_X + COLW + 10)
emb('W2', 'K', 'R', COL2_X + COLW + 20)
emb('I1', 'N', 'R', COL2_X + COLW + 10)
emb('D1', 'S', 'R', COL2_X + COLW + 10)
emb('S1', 'G', 'R', COL2_X + COLW + 20)
emb('S2', 'T', 'R', COL2_X + COLW + 10)
# N1 / Q1 linkage (dashed, not embedding)
x, ry = ROWPOS['N1']; qx, qy = ROWPOS['Q1']
add(f'<path class="lnk" d="M{qx},{qy + 8} L{COL2_X - 12},{qy + 8} L{COL2_X - 12},{ry + 8} L{x},{ry + 8}"/>')
t(COL2_X - 16, (qy + ry) / 2 + 12, 'first/next 串成链表', 'capr', ' transform="rotate(-90 {:.1f} {:.1f})"'.format(COL2_X - 16, (qy + ry) / 2 + 12))

H_TOTAL = int(max(LEFT_BOTTOM, colY[1], colY[2]) + 20)

head = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W_TOTAL} {H_TOTAL}" width="{W_TOTAL}" height="{H_TOTAL}"
     font-family="PingFang SC, Hiragino Sans GB, Noto Sans CJK SC, Microsoft YaHei, sans-serif">
<!-- irq_work 路径：函数归属、结构体全部字段、函数读写了哪些字段（由 gen_fields.py 生成） -->
<defs>
  <marker id="ahe" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#555"/></marker>
</defs>
<style>
  text  {{ fill:#000; }}
  .ttl  {{ font-size:17px; font-weight:bold; }}
  .ph   {{ font-size:12.5px; font-weight:bold; }}
  .cap  {{ font-size:10.5px; fill:#333; }}
  .capr {{ font-size:10.5px; fill:#333; text-anchor:middle; }}
  .snt  {{ fill:#fff; font-size:10px; font-weight:bold; text-anchor:middle; }}
  .fnm  {{ font-family:"SF Mono",Menlo,Consolas,"Courier New",monospace; font-size:11.5px; font-weight:bold; }}
  .otag {{ fill:#fff; font-size:9.5px; font-weight:bold; }}
  .loc  {{ font-family:"SF Mono",Menlo,Consolas,"Courier New",monospace; font-size:9.5px; fill:#555; }}
  .dsc  {{ font-size:11px; }}
  .chip {{ font-family:"SF Mono",Menlo,Consolas,"Courier New",monospace; font-size:9.5px; }}
  .sth  {{ font-family:"SF Mono",Menlo,Consolas,"Courier New",monospace; font-size:11px; font-weight:bold; }}
  .fid  {{ fill:#fff; font-size:8.5px; font-weight:bold; text-anchor:middle; }}
  .fname{{ font-family:"SF Mono",Menlo,Consolas,"Courier New",monospace; font-size:10px; font-weight:bold; }}
  .ftype{{ font-family:"SF Mono",Menlo,Consolas,"Courier New",monospace; font-size:9px; fill:#666; }}
  .fmean{{ font-size:10.5px; }}
  .mk   {{ font-size:8.5px; font-weight:bold; text-anchor:middle; }}
  .emb  {{ fill:none; stroke:#555; stroke-width:1.1; marker-end:url(#ahe); }}
  .lnk  {{ fill:none; stroke:#009E73; stroke-width:1.1; stroke-dasharray:4 3; }}
</style>
<rect width="{W_TOTAL}" height="{H_TOTAL}" fill="#fff"/>
'''

legend = []
def L(s): legend.append(s)
L(f'<text class="ttl" x="20" y="32">irq_work 路径（proxy 写）：函数归属 · 结构体全部字段 · 谁读写了哪个字段</text>')
lx = 20; ly = 56
for k in ('E', 'L', 'D'):
    fill, stroke, lab = OWN[k]
    L(f'<rect x="{lx}" y="{ly}" width="30" height="16" rx="2" fill="{fill}" stroke="{stroke}" stroke-width="1.2"/><rect x="{lx}" y="{ly}" width="5" height="16" fill="{stroke}"/>')
    desc = {'E': 'EVL 的函数/结构体（kernel/evl, include/evl）',
            'L': 'Linux 原有（irq_work、workqueue、VFS）',
            'D': 'Dovetail 加进 Linux 的（kernel/irq/pipeline.c 等）'}[k]
    L(f'<text class="dsc" x="{lx + 38}" y="{ly + 12}">{escape(desc)}</text>')
    lx += 38 + wcn(desc, 11) + 26
lx = 20; ly = 84
for mode, lab in (('W', '写'), ('RW', '读写'), ('R', '读')):
    bg, bd, fg = MODE_STYLE[mode]
    L(f'<rect x="{lx}" y="{ly}" width="58" height="15" rx="2" fill="{bg}" stroke="{bd}"/><text class="chip" x="{lx + 6}" y="{ly + 11}" style="fill:{fg}">P6 wroff</text>')
    L(f'<text class="dsc" x="{lx + 64}" y="{ly + 12}">{lab}</text>')
    lx += 64 + wcn(lab, 11) + 24
L(f'<text class="cap" x="{lx}" y="{ly + 12}">左边每个函数下方的标签 = 它读写的字段（编号对应右边结构体）；右边每个字段行末的小方块 = 读写它的步骤号，颜色含义相同。</text>')
L(f'<text class="cap" x="20" y="{ly + 32}">灰色箭头：结构体嵌入或指针指向（如 P12 relay_work 就是一个 evl_work）。绿色虚线：N1 next 和 Q1 first 串成 raised_list 链表。步骤 i1–i5 在创建时执行一次；1–24 是每次 oob 写的路径。</text>')
L(f'<text class="ph" x="{COL1_X}" y="140">结构体与字段</text>')
L(f'<text class="ph" x="20" y="140">函数（按执行顺序）</text>')

with open(OUT, 'w') as fp:
    fp.write(head + '\n'.join(legend) + '\n' + '\n'.join(o) + '\n</svg>\n')
print(W_TOTAL, H_TOTAL)
