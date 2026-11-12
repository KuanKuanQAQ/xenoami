#!/usr/bin/env python3
"""Generate fig4 (original xbuf obnd path) and fig5 (trusted-end design).

Plain SVG only: inline attributes, no CSS, no markers, one <text> per
line, so that PowerPoint "Convert to Shape" keeps everything editable.
"""
import math, os, sys

OUT = os.path.dirname(os.path.abspath(__file__))
FONT = "Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif"
MONO = "Consolas, Menlo, Courier New, monospace"
BLUE, BLUEF, BLUEBG = "#0072B2", "#E3EEF7", "#EEF3F8"
RED, REDF, REDBG = "#D55E00", "#FBEBE1", "#FDF3EC"
AMBER, GREEN = "#B07A00", "#2E7D32"
GRAY, GRAYBG = "#666666", "#F5F5F5"


class Svg:
    def __init__(s, w, h, title):
        s.w, s.h, s.o = w, h, []
        s.o.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
                   f'viewBox="0 0 {w} {h}" font-family="{FONT}">')
        s.o.append(f"<!-- {title} -->")
        s.rect(0, 0, w, h, "#FFFFFF")

    def add(s, x):
        s.o.append(x)

    def rect(s, x, y, w, h, fill="none", stroke=None, sw=1.2, rx=0, dash=None):
        a = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        if dash:
            a += f' stroke-dasharray="{dash}"'
        s.add(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx:g}" fill="{fill}"{a}/>')

    def circle(s, cx, cy, r, fill="none", stroke=None, sw=1.2):
        a = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        s.add(f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{fill}"{a}/>')

    def line(s, x1, y1, x2, y2, stroke="#555555", sw=1.2, dash=None):
        a = f' stroke-dasharray="{dash}"' if dash else ""
        s.add(f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" stroke="{stroke}" stroke-width="{sw}"{a}/>')

    def path(s, d, fill="none", stroke=None, sw=1.2, dash=None):
        a = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        if dash:
            a += f' stroke-dasharray="{dash}"'
        s.add(f'<path d="{d}" fill="{fill}"{a}/>')

    def head(s, x, y, ang, color, size=8):
        # triangle with tip at (x, y) pointing along ang (radians)
        b1 = (x - size * math.cos(ang) + size * 0.5 * math.sin(ang),
              y - size * math.sin(ang) - size * 0.5 * math.cos(ang))
        b2 = (x - size * math.cos(ang) - size * 0.5 * math.sin(ang),
              y - size * math.sin(ang) + size * 0.5 * math.cos(ang))
        s.add(f'<polygon points="{x:.1f},{y:.1f} {b1[0]:.1f},{b1[1]:.1f} {b2[0]:.1f},{b2[1]:.1f}" fill="{color}"/>')

    def arrow(s, x1, y1, x2, y2, color="#555555", sw=1.3, dash=None, size=8):
        ang = math.atan2(y2 - y1, x2 - x1)
        ex, ey = x2 - (size - 1) * math.cos(ang), y2 - (size - 1) * math.sin(ang)
        s.line(x1, y1, ex, ey, color, sw, dash)
        s.head(x2, y2, ang, color, size)

    def poly_arrow(s, pts, color, sw=1.3, dash=None, size=8):
        # orthogonal polyline with head at the last point
        (xa, ya), (xb, yb) = pts[-2], pts[-1]
        ang = math.atan2(yb - ya, xb - xa)
        pts = pts[:-1] + [(xb - (size - 1) * math.cos(ang), yb - (size - 1) * math.sin(ang))]
        d = "M" + " L".join(f"{x:g},{y:g}" for x, y in pts)
        s.path(d, "none", color, sw, dash)
        s.head(xb, yb, ang, color, size)

    def text(s, x, y, t, size=13, fill="#000000", anchor="start", bold=False, mono=False, italic=False):
        a = f' font-weight="bold"' if bold else ""
        if mono:
            a += f' font-family="{MONO}"'
        if italic:
            a += ' font-style="italic"'
        t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        s.add(f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" fill="{fill}" text-anchor="{anchor}"{a}>{t}</text>')

    def save(s, name):
        s.o.append("</svg>\n")
        with open(os.path.join(OUT, name), "w") as f:
            f.write("\n".join(s.o))


# ------------------------------------------------------------------ icons
def ic_file(s, cx, cy, c=RED):
    x, y = cx - 13, cy - 17
    s.path(f"M{x},{y} h18 l8,8 v26 h-26 z", "#FFFFFF", c, 1.4)
    s.path(f"M{x+18},{y} v8 h8", "none", c, 1.2)
    for i in range(3):
        s.line(x + 5, y + 15 + i * 6, x + 21, y + 15 + i * 6, c, 1)


def ic_person(s, cx, cy, c=RED):
    s.circle(cx, cy - 8, 7, "#FFFFFF", c, 1.4)
    s.path(f"M{cx-13},{cy+16} q0,-15 13,-15 q13,0 13,15 z", "#FFFFFF", c, 1.4)


def ic_waitq(s, cx, cy, c=RED, n=3, label=None):
    s.line(cx - 34, cy - 10, cx + 34, cy - 10, c, 2)
    for i in range(n):
        x = cx - 20 + i * 20
        s.line(x, cy - 10, x, cy - 2, c, 1)
        s.circle(x, cy + 5, 7, "#FFFFFF", c, 1.3)


def ic_pages(s, cx, cy, c=RED):
    for r in range(3):
        for k in range(4):
            x, y = cx - 25 + k * 13, cy - 18 + r * 13
            hot = (r, k) in ((1, 2),)
            s.rect(x, y, 11, 11, REDF if not hot else c, c, 1)


def ic_irq(s, cx, cy, c=RED):
    s.path(f"M{cx+3},{cy-18} L{cx-9},{cy+2} h9 l-4,16 l14,-21 h-9 l5,-15 z", "#FFFFFF", c, 1.4)


def ic_lock(s, cx, cy, c, open_=False):
    if open_:
        s.path(f"M{cx-8},{cy-4} v-8 a8,8 0 0 1 16,0 v-3", "none", c, 2.2)
    else:
        s.path(f"M{cx-8},{cy-4} v-8 a8,8 0 0 1 16,0 v8", "none", c, 2.2)
    s.rect(cx - 13, cy - 4, 26, 20, c, None, rx=3)
    s.circle(cx, cy + 5, 3, "#FFFFFF")


def ic_rt(s, cx, cy, sleeping=True):
    s.circle(cx, cy, 13, "#FFFFFF", BLUE, 1.6)
    s.text(cx, cy + 4.5, "RT", 11, BLUE, "middle", bold=True)
    if sleeping:
        s.text(cx + 15, cy - 9, "z", 10, GRAY, italic=True)
        s.text(cx + 21, cy - 16, "z", 12, GRAY, italic=True)


def ic_runq(s, cx, cy, c=BLUE):
    for i, w in enumerate((44, 34, 24)):
        s.rect(cx - 22, cy - 16 + i * 11, w, 8, BLUEF if i else c, c, 1, rx=2)


def ic_clock(s, cx, cy, c):
    s.circle(cx, cy, 11, "#FFFFFF", c, 1.6)
    s.line(cx, cy, cx, cy - 7, c, 1.6)
    s.line(cx, cy, cx + 5, cy + 3, c, 1.6)


def ic_bell(s, cx, cy, c):
    s.path(f"M{cx-10},{cy+6} q2,-2 2,-8 a8,8 0 0 1 16,0 q0,6 2,8 z", "#FFFFFF", c, 1.5)
    s.circle(cx, cy + 9, 2.5, c)


def ring(s, cx, cy, ro, ri, nseg, filled, fillc, strokec, pointers):
    for i in range(nseg):
        a0 = -math.pi / 2 + i * 2 * math.pi / nseg
        a1 = a0 + 2 * math.pi / nseg
        p = [(cx + ro * math.cos(a0), cy + ro * math.sin(a0)), (cx + ro * math.cos(a1), cy + ro * math.sin(a1)),
             (cx + ri * math.cos(a1), cy + ri * math.sin(a1)), (cx + ri * math.cos(a0), cy + ri * math.sin(a0))]
        d = (f"M{p[0][0]:.1f},{p[0][1]:.1f} A{ro},{ro} 0 0 1 {p[1][0]:.1f},{p[1][1]:.1f} "
             f"L{p[2][0]:.1f},{p[2][1]:.1f} A{ri},{ri} 0 0 0 {p[3][0]:.1f},{p[3][1]:.1f} Z")
        s.path(d, fillc if i in filled else "#FFFFFF", strokec, 1)
    for idx, name, col, side in pointers:
        a = -math.pi / 2 + idx * 2 * math.pi / nseg
        x1, y1 = cx + (ro + 22) * math.cos(a), cy + (ro + 22) * math.sin(a)
        x2, y2 = cx + (ro + 2) * math.cos(a), cy + (ro + 2) * math.sin(a)
        s.arrow(x1, y1, x2, y2, col, 1.6, size=7)
        tx, ty = cx + (ro + 28) * math.cos(a), cy + (ro + 28) * math.sin(a) + 4
        s.text(tx, ty, name, 11, col, "start" if side == "r" else "end", mono=True, bold=True)


def gauge(s, x, y, w, h, frac, c, thresh=None):
    s.rect(x, y, w, h, "#FFFFFF", c, 1.2, rx=2)
    s.rect(x + 1, y + 1, (w - 2) * frac, h - 2, c, None, rx=1)
    if thresh is not None:
        tx = x + w * thresh
        s.line(tx, y - 4, tx, y + h + 4, RED, 1.6)


def step(s, x, y, n, c="#333333"):
    s.circle(x, y, 11, c)
    s.text(x, y + 4.5, str(n), 12, "#FFFFFF", "middle", bold=True)


def hatch(s, x, y, w, h, c):
    s.rect(x, y, w, h, "#FFFFFF", c, 1)
    k = -h
    while k < w:
        x1, y1 = x + max(k, 0), y + h - max(0, -k) if k < 0 else y + h
        x1 = x + max(k, 0)
        y1 = y + h - (max(k, 0) - k)
        x2 = x + min(k + h, w)
        y2 = y + h - (min(k + h, w) - k)
        s.line(x1, y1, x2, y2, c, 0.9)
        k += 7


# ------------------------------------------------------------------ bands
W, H = 1184, 650
B1, B2, B3 = (8, 152), (164, 400), (412, 612)


def bands(s, mid_label, mid_code):
    s.rect(4, B1[0], W - 8, B1[1] - B1[0], REDBG, rx=8)
    s.rect(4, B2[0], W - 8, B2[1] - B2[0], GRAYBG, rx=8)
    s.rect(4, B3[0], W - 8, B3[1] - B3[0], BLUEBG, rx=8)
    s.text(18, 30, "Linux 对象：不可信", 14, RED, bold=True)
    s.text(18, 48, "红字：会改写它的函数", 11, GRAY)
    s.text(18, B2[1] - 26, mid_label, 14, "#000000", bold=True)
    s.text(18, B2[1] - 9, mid_code, 11, GRAY, mono=True)
    s.text(18, B3[1] - 10, "oob 对象：应当可信", 14, BLUE, bold=True)


OBJ_Y = 64


def linux_objects(s, last):
    objs = [
        (250, ic_file, "struct file", "f_flags · private_data", "fcntl · dup2 · close"),
        (420, ic_person, "current", "signal_pending · 状态", "kill · ptrace · 调度器"),
        (590, ic_waitq, "wait_queue_head", None, "wait_event · wake_up"),
        (790, ic_pages, "u_buf 的页表 / VMA", "用户缓冲区", "mmap · munmap · userfaultfd"),
        last,
    ]
    for x, ic, name, field, writers in objs:
        ic(s, x, OBJ_Y - 22)
        s.text(x, 78, name, 13, "#000000", "middle", bold=True)
        if field:
            s.text(x, 98, field, 11, "#333333", "middle", mono=field.isascii())
        s.text(x, 140, writers, 12, RED, "middle")
    return objs


def step_block(s, x, n, name, code, axis_y, c="#333333", w=None):
    s.text(x, 216, name, 13, "#000000", "middle", bold=True)
    for i, l in enumerate(code):
        s.text(x, 236 + i * 15, l, 11, "#222222", "middle", mono=True)
    step(s, x, axis_y, n, c)


def legend(s, items, y=636):
    x = 18
    for kind, color, label in items:
        if kind == "solid":
            s.arrow(x, y - 4, x + 34, y - 4, color, 1.8, size=7)
        elif kind == "dash":
            s.arrow(x, y - 4, x + 34, y - 4, color, 1.3, dash="4 3", size=7)
        x += 42
        s.text(x, y, label, 12, "#333333")
        x += 13 * len(label) + 30


# ================================================================= fig4
def fig4():
    s = Svg(W, H, "原实现：xbuf 出站环（inband 写 → oob 读）上，inband 写路径如何触及 oob")
    bands(s, "inband 写路径（非实时域）", "xbuf_write() → do_xbuf_write(&obnd.ring)")
    linux_objects(s, (1060, ic_irq, "irq_work 每 CPU 链表", "obnd.irq_work", "Linux irq_work 处理"))

    AX = 290
    s.arrow(36, AX, 1160, AX, "#999999", 1.4)
    steps = [
        (105, "满则睡眠", ["wait_event_interruptible(", "obnd.o_event, …)"]),
        (270, "加锁", ["ring->lock():", "obnd.i_event.wchan.lock"]),
        (440, "预留空间", ["wroff += len", "wrpending++; wrrsvd += len"]),
        (630, "放锁，拷贝数据", ["raw_copy_from_user(", "bufmem + wroff, u_buf)"]),
        (820, "重新加锁，发布", ["wrpending--", "fillsz += wrrsvd"]),
        (985, "唤醒 oob 读者", ["evl_flush_wait_locked(", "&obnd.i_event)"]),
        (1110, "放锁，调度", ["evl_schedule()"]),
    ]
    for i, (x, name, code) in enumerate(steps):
        step_block(s, x, i, name, code, AX)

    # lock-held bars and reservation window
    s.text(18, 315, "持 oob 锁", 11, RED, bold=True)
    s.rect(270, 306, 360, 8, RED, rx=2)
    s.rect(820, 306, 290, 8, RED, rx=2)
    s.text(18, 337, "预留窗口", 11, AMBER, bold=True)
    s.line(440, 330, 820, 330, AMBER, 1.6)
    s.line(440, 324, 440, 336, AMBER, 1.6)
    s.line(820, 324, 820, 336, AMBER, 1.6)
    s.text(630, 350, "wrpending > 0：oob 读者看不到这批数据", 11, AMBER, "middle")
    hatch(s, 588, 357, 84, 12, RED)
    s.text(630, 386, "缺页 → Linux 缺页处理；userfaultfd 可让它无限期挂起", 11, RED, "middle", bold=True)

    # dependencies on Linux objects (read)
    for x0, x1, y1 in ((250, 100, 202), (420, 116, 202), (590, 132, 202), (790, 640, 202)):
        s.arrow(x0, 150, x1, y1, "#888888", 1.2, dash="4 3", size=7)

    # writes into oob objects
    # lock
    ic_lock(s, 270, 462, BLUE)
    s.text(270, 497, "obnd.i_event.wchan.lock", 11, BLUE, "middle", mono=True, bold=True)
    s.text(270, 513, "hard spinlock，同时是 oob 读者", 11, "#333333", "middle")
    s.text(270, 528, "等待队列的锁", 11, "#333333", "middle")
    # ring
    ring(s, 488, 512, 50, 28, 12, {3, 4, 5, 6}, "#9CC3E0", BLUE,
         [(3, "rdoff", BLUE, "r"), (7, "wroff", RED, "l")])
    s.text(488, 516, "bufmem", 10, "#333333", "middle", mono=True)
    # fields
    s.text(735, 448, "fillsz", 12, "#000000", mono=True, bold=True)
    gauge(s, 790, 437, 120, 14, 0.45, BLUE)
    for i, f in enumerate(("wrrsvd", "wrpending", "rdrsvd · rdpending")):
        s.text(735, 476 + i * 18, f, 11, "#333333", mono=True)
    s.text(735, 542, "全部位于 struct xbuf_ring", 11, GRAY)
    # oob reader
    ic_waitq(s, 985, 458, BLUE, 1)
    ic_rt(s, 985, 470)
    s.text(985, 505, "obnd.i_event", 11, BLUE, "middle", mono=True, bold=True)
    s.text(985, 521, "oob 读者在此睡眠", 11, "#333333", "middle")
    # scheduler
    ic_runq(s, 1110, 452)
    s.text(1110, 497, "EVL 调度器", 11, BLUE, "middle", bold=True)
    # function pointers
    s.rect(30, 432, 150, 70, "#FFFFFF", BLUE, 1, rx=4)
    for i, f in enumerate(("ring->lock()", "ring->wait_*()", "ring->signal_*()")):
        s.text(42, 452 + i * 19, f, 11, "#222222", mono=True)
    s.text(30, 520, "同一份 do_xbuf_*() 两域共用，", 11, GRAY)
    s.text(30, 535, "经函数指针分派", 11, GRAY)

    # red arrows: inband code writing oob objects / calling oob code
    for x0, x1, y1, lab in ((270, 270, 440, "取锁"), (440, 470, 456, "改 wroff"),
                           (820, 850, 434, "改 fillsz"), (985, 985, 444, "直接唤醒"),
                           (1110, 1110, 440, "调度")):
        s.arrow(x0, 397, x1, y1, RED, 2, size=9)
        s.text(max(x0, x1) + 8, 428, lab, 11, RED, bold=True)
    s.arrow(630, 397, 520, 470, "#888888", 1.4, size=8)
    s.text(588, 446, "写数据", 11, GRAY)

    # oob code touching a Linux object: irq_work_queue -> wake_up
    s.poly_arrow([(1000, 470), (1168, 470), (1168, OBJ_Y - 22), (1092, OBJ_Y - 22)], BLUE, 1.4, dash="5 3")
    s.text(1160, 566, "oob 读者读走数据后：", 11, BLUE, "end")
    s.text(1160, 582, "irq_work_queue(&obnd.irq_work)", 11, BLUE, "end", mono=True)
    s.text(1160, 598, "→ inband 再 wake_up(o_event)", 11, BLUE, "end")

    legend(s, [("solid", RED, "inband 代码写 oob 对象、调用 oob 代码"),
               ("dash", "#888888", "依赖 Linux 对象"),
               ("dash", BLUE, "oob 代码操作 Linux 对象"),
               ("solid", "#888888", "写共享数据（内容本就不可信）")])
    s.save("fig4-xbuf-orig.svg")


# ================================================================= fig5
def fig5():
    s = Svg(W, H, "改后（方案三）：可信端自持状态，不可信端只报告结果")
    bands(s, "inband 写路径（非实时域）", "xbuf_write()")
    linux_objects(s, (1060, ic_irq, "合成中断处理", "evl_notify_handler()", "Linux 中断子系统"))

    AX = 290
    s.arrow(36, AX, 700, AX, "#999999", 1.4)
    steps = [
        (105, "满则睡眠", ["wait_event_interruptible(", "ib_wait, …)"]),
        (280, "取写位置", ["off = ib_wroff", "（不可信侧自己的副本）"]),
        (460, "拷贝数据", ["copy_from_user(", "bufmem + off, u_buf)"]),
        (640, "报告结果", ["xbuf_gate_commit(h, n)"]),
    ]
    for i, (x, name, code) in enumerate(steps):
        step_block(s, x, i, name, code, AX)
    s.text(18, 315, "持 oob 锁", 11, GREEN, bold=True)
    s.text(105, 315, "无：全程只用 inband 自己的锁和等待队列", 11, GREEN)
    hatch(s, 418, 334, 84, 12, "#999999")
    s.text(460, 364, "缺页只拖慢它自己", 11, GRAY, "middle", bold=True)

    for x0, x1, y1 in ((250, 100, 202), (420, 116, 202), (590, 132, 202), (790, 470, 202)):
        s.arrow(x0, 150, x1, y1, "#888888", 1.2, dash="4 3", size=7)

    # what an untrusted side can still do
    px = 760
    s.text(px, 214, "不可信端还能做什么", 13, "#000000", bold=True)
    rows = [("报告假的 n", "截断到空闲空间内：只影响内容"),
            ("不停地调门", "只在条件变化时唤醒；门内有界"),
            ("从不调门", "oob 读者等待超时，告警")]
    for i, (a, b) in enumerate(rows):
        y = 244 + i * 34
        s.rect(px, y - 15, 120, 22, REDF, RED, 1, rx=11)
        s.text(px + 60, y, a, 12, RED, "middle", bold=True)
        s.arrow(px + 126, y - 4, px + 156, y - 4, "#555555", 1.3, size=7)
        s.text(px + 164, y, b, 12, "#000000")

    # the gate
    gx, gy, gw, gh = 530, 424, 290, 150
    s.rect(gx, gy, gw, gh, "#FFFFFF", BLUE, 1.6, rx=8)
    s.text(gx + 14, gy + 22, "门 xbuf_gate_commit()：可信代码执行", 12, BLUE, bold=True)
    ic_lock(s, gx + 28, gy + 62, BLUE)
    code = ["lock(可信锁)", "n = min(n, bufsz − fill)", "wroff += n;  fill += n",
            "读者在等 → evl_flush_wait()", "unlock"]
    for i, l in enumerate(code):
        s.text(gx + 56, gy + 46 + i * 18, l, 11, "#222222", mono=True)
    s.text(gx + 14, gy + gh - 10, "有界 · 不碰用户内存 · 不调不可信代码", 11, GREEN, bold=True)
    s.arrow(640, 302, 640, gy - 2, "#333333", 2, size=9)
    s.text(648, 410, "只传入 n", 11, "#333333", bold=True)

    # trusted-owned ring
    ring(s, 330, 512, 50, 28, 12, {3, 4, 5, 6}, "#9CC3E0", BLUE,
         [(3, "rdoff", BLUE, "r"), (7, "wroff", BLUE, "l")])
    s.text(330, 516, "bufmem", 10, "#333333", "middle", mono=True)
    s.text(30, 470, "下标、fill", 12, BLUE, bold=True)
    s.text(30, 488, "只由可信代码修改", 12, BLUE, bold=True)
    s.arrow(450, 374, 356, 466, "#888888", 1.4, size=8)
    s.text(414, 428, "写数据", 11, GRAY)
    s.arrow(gx, 470, 377, 481, BLUE, 1.8, size=8)
    s.text(450, 464, "更新", 11, BLUE, bold=True)

    # oob reader with timeout
    rx = 960
    ic_waitq(s, rx, 458, BLUE, 1)
    ic_rt(s, rx, 470)
    ic_clock(s, rx + 44, 440, AMBER)
    s.text(rx, 505, "obnd.i_event", 11, BLUE, "middle", mono=True, bold=True)
    s.text(rx, 521, "带超时等待，超时则告警", 11, AMBER, "middle", bold=True)
    s.arrow(gx + gw, 500, rx - 16, 476, BLUE, 1.8, size=8)
    s.text(868, 476, "唤醒", 11, BLUE, bold=True)

    # reverse notification via relay
    s.rect(1088, 540, 76, 40, "#FFFFFF", BLUE, 1, rx=4)
    for i in range(3):
        s.rect(1096 + i * 22, 550, 16, 20, BLUEF, BLUE, 1, rx=2)
    s.text(1126, 596, "可信每 CPU 链表", 11, BLUE, "middle")
    s.poly_arrow([(rx + 13, 470), (1126, 470), (1126, 540)], BLUE, 1.3, size=7)
    s.poly_arrow([(1164, 560), (1172, 560), (1172, OBJ_Y - 22), (1092, OBJ_Y - 22)], BLUE, 1.4, dash="5 3")
    s.text(845, 550, "读者读走数据后：", 11, BLUE)
    s.text(845, 566, "evl_inband_notify_post()", 11, BLUE, mono=True)
    s.text(845, 582, "只记录，发合成中断；", 11, BLUE)
    s.text(845, 598, "inband 取走后自己 wake_up", 11, BLUE)

    legend(s, [("solid", BLUE, "可信代码修改可信状态"),
               ("solid", "#333333", "不可信端只传入数值"),
               ("dash", "#888888", "依赖 Linux 对象"),
               ("dash", BLUE, "通知：可信端只记录，不调用"),
               ("solid", "#888888", "写共享数据")])
    s.save("fig5-xbuf-new.svg")


fig4()
fig5()
