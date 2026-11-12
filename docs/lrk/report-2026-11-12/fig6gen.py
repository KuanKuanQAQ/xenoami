#!/usr/bin/env python3
"""fig6: pipe flow + marks, who writes / reads each mark under Z/z, for
xbuf (A: oob->ib, B: ib->oob) and proxy (output, input), with
proxy_write()/proxy_read() dropped: only oob callers use the rings, the
relay is the only in-band party.  fig7: the same marks after the cut.

Facts follow the original kernel/evl/xbuf.c and proxy.c:
- space / data checks use fillsz and the local reservation only
  (xbuf: fillsz + wrrsvd, fillsz - rdrsvd; proxy: fillsz + reserved);
- wroff is producer-only and rdoff consumer-only;
- the proxy relay never takes ring->lock (worker_lock mutex instead), so
  once in-band callers are gone the ring lock is Z-only.
"""
import math, os

OUT = os.path.dirname(os.path.abspath(__file__))
FONT = "Microsoft YaHei, PingFang SC, Noto Sans CJK SC, sans-serif"
MONO = "Consolas, Menlo, Courier New, monospace"
BLUE, RED, GRAY, INK = "#0072B2", "#D55E00", "#666666", "#222222"
BLUE_F, RED_F = "#E3EEF7", "#FBEBE1"

CH = {  # chip kind -> (fill, stroke)
    "a": ("#FCEFD2", "#B07A00"),   # fig6: Z is exposed to z
    "v": ("#ECE6F5", "#7A5C99"),   # fig6: Z has to notify z
    "n": ("#F2F2F2", "#999999"),   # fig6: one domain only, or Z writes / z reads
    "na": ("#FFFFFF", "#DDDDDD"),  # no such mark here
    "el": ("#E6E6E6", "#555555"),  # fig7: eliminated
    "cp": ("#E3F1E4", "#2E7D32"),  # fig7: private copy
    "in": ("#FCEFD2", "#B07A00"),  # fig7: input only
    "rc": ("#ECE6F5", "#7A5C99"),  # fig7: record, do not call
    "ow": ("#DDF0F3", "#00838F"),  # fig7: owned by one side
    "ok": ("#FFFFFF", "#BBBBBB"),  # fig7: was never in the intersection
}


class Svg:
    def __init__(s, w, h, comment):
        s.w, s.h, s.o = w, h, []
        s.add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
              f'viewBox="0 0 {w} {h}" font-family="{FONT}">')
        s.add(f"<!-- {comment} -->")
        s.rect(0, 0, w, h, "#FFFFFF")

    def add(s, x):
        s.o.append(x)

    def save(s, name):
        s.add("</svg>\n")
        with open(os.path.join(OUT, name), "w") as f:
            f.write("\n".join(s.o))

    @staticmethod
    def esc(t):
        return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def rect(s, x, y, w, h, fill="none", stroke=None, sw=1.2, rx=0, dash=None):
        a = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        if dash:
            a += f' stroke-dasharray="{dash}"'
        s.add(f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx:g}" fill="{fill}"{a}/>')

    def circle(s, cx, cy, r, fill, stroke=None, sw=1.2):
        a = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        s.add(f'<circle cx="{cx:g}" cy="{cy:g}" r="{r:g}" fill="{fill}"{a}/>')

    def line(s, x1, y1, x2, y2, c="#555555", sw=1.2, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        s.add(f'<line x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" stroke="{c}" stroke-width="{sw}"{d}/>')

    def path(s, d, fill="none", stroke=None, sw=1.2):
        a = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
        s.add(f'<path d="{d}" fill="{fill}"{a}/>')

    def head(s, x, y, ang, c, z=8):
        b1 = (x - z * math.cos(ang) + z * .5 * math.sin(ang), y - z * math.sin(ang) - z * .5 * math.cos(ang))
        b2 = (x - z * math.cos(ang) - z * .5 * math.sin(ang), y - z * math.sin(ang) + z * .5 * math.cos(ang))
        s.add(f'<polygon points="{x:.1f},{y:.1f} {b1[0]:.1f},{b1[1]:.1f} {b2[0]:.1f},{b2[1]:.1f}" fill="{c}"/>')

    def arrow(s, pts, c="#333333", sw=1.4, z=8, dash=None):
        (xa, ya), (xb, yb) = pts[-2], pts[-1]
        ang = math.atan2(yb - ya, xb - xa)
        p = pts[:-1] + [(xb - (z - 1) * math.cos(ang), yb - (z - 1) * math.sin(ang))]
        d = f' stroke-dasharray="{dash}"' if dash else ""
        s.add(f'<path d="M{" L".join(f"{x:g},{y:g}" for x, y in p)}" fill="none" stroke="{c}" stroke-width="{sw}"{d}/>')
        s.head(xb, yb, ang, c, z)

    def text(s, x, y, t, size=13, fill="#000000", anchor="start", bold=False, mono=False):
        a = ' font-weight="bold"' if bold else ""
        if mono:
            a += f' font-family="{MONO}"'
        s.add(f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" fill="{fill}" text-anchor="{anchor}"{a}>{s.esc(t)}</text>')

    def ztext(s, x, y, t, size=13, anchor="start", bold=False, base=INK):
        """Text in which every Z is blue and every z vermillion."""
        parts, cur = [], ""
        word = lambda c: c.isascii() and (c.isalnum() or c in "_′'")
        for i, ch in enumerate(t):
            alone = not (i > 0 and word(t[i - 1])) and not (i + 1 < len(t) and word(t[i + 1]))
            if ch in "Zz" and alone:
                if cur:
                    parts.append((cur, base))
                    cur = ""
                parts.append((ch, BLUE if ch == "Z" else RED))
            else:
                cur += ch
        if cur:
            parts.append((cur, base))
        a = ' font-weight="bold"' if bold else ""
        spans = "".join(f'<tspan fill="{c}">{s.esc(p)}</tspan>' for p, c in parts)
        s.add(f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" text-anchor="{anchor}"{a}>{spans}</text>')

    # ---------------------------------------------------------- icons
    def flag(s, x, y, c="#333333"):
        s.line(x, y, x, y + 30, c, 2)
        s.add(f'<polygon points="{x},{y} {x+20},{y+7} {x},{y+14}" fill="{c}"/>')

    def lock(s, cx, cy, c):
        s.path(f"M{cx-7},{cy-3} v-7 a7,7 0 0 1 14,0 v7", "none", c, 2)
        s.rect(cx - 11, cy - 3, 22, 17, c, rx=3)
        s.circle(cx, cy + 5, 2.6, "#FFFFFF")

    def gate(s, cx, cy, c):
        s.rect(cx - 16, cy - 16, 6, 32, c)
        s.rect(cx + 10, cy - 16, 6, 32, c)
        s.rect(cx - 20, cy - 21, 40, 6, c)

    def records(s, cx, cy, c):
        for i in range(3):
            s.rect(cx - 25 + i * 18, cy - 11, 14, 22, "#FFFFFF", c, 1.2, 2)

    def chip(s, x, y, w, h, kind, label, size=12):
        r = h / 2
        if kind == "cpin":   # private copy + input: two halves
            g, a = CH["cp"], CH["in"]
            mx = x + w / 2
            s.path(f"M{mx},{y} H{x+r} A{r},{r} 0 0 0 {x+r},{y+h} H{mx} Z", g[0])
            s.path(f"M{mx},{y} H{x+w-r} A{r},{r} 0 0 1 {x+w-r},{y+h} H{mx} Z", a[0])
            s.rect(x, y, w, h, "none", "#777777", 1.2, r)
        else:
            f, st = CH[kind]
            s.rect(x, y, w, h, f, st, 1.2, r)
        base = "#999999" if kind == "na" else INK
        s.ztext(x + w / 2, y + h / 2 + size * 0.36, label, size, "middle", bold=(kind != "na"), base=base)

    def step(s, cx, cy, label):
        s.circle(cx, cy, 11, "#333333")
        s.text(cx, cy + 4, label, 10, "#FFFFFF", "middle", bold=True)

    def legend(s, y, items, right=None):
        x = 20
        for k, lab in items:
            s.chip(x, y - 15, 34, 20, k, "", 1)
            s.ztext(x + 44, y, lab, 13)
            x += 44 + 13.2 * len(lab) + 34
        if right:
            s.ztext(s.w - 20, y, right, 12.5, "end", base=GRAY)


COLS_HEAD = (("xbuf A 向 oob → ib", "P = Z，C = z"),
             ("xbuf B 向 ib → oob", "P = z，C = Z"),
             ("proxy 输出", "P = Z，C = z（转发者）"),
             ("proxy 输入", "P = z（转发者），C = Z"))


def table_head(s, cols, y, first):
    s.rect(20, y, s.w - 40, 50, "#F2F2F2")
    for (x, w), h in zip(cols[:len(first)], first):
        s.text(x + 10, y + 31, h, 13.5, bold=True)
    for (x, w), (h1, h2) in zip(cols[len(first):], COLS_HEAD):
        s.text(x + w / 2, y + 21, h1, 13.5, anchor="middle", bold=True)
        s.ztext(x + w / 2, y + 40, h2, 12, "middle", bold=True)


# ====================================================================== fig6
def fig6():
    W, H = 1500, 1150
    s = Svg(W, H, "管道的流程与标记；每个标记谁写谁读，代入 Z / z")
    CX = W / 2
    s.text(20, 30, "① 一个方向的管道：流程与必需的标记（按原实现）", 17, bold=True)

    LY, LH, LW = 54, 316, 360
    lanes = (
        (20, "生产者 P（可能多个）", [
            ("P1", "加锁，查空间：fillsz + 本侧 rsvd + n ≤ bufsz？"),
            ("P2", "放不下：放锁，在「有空间」上等"),
            ("P3", "预留：off = W；W += n；pending++；rsvd += n"),
            ("P4", "放锁，写数据到 buf[off]"),
            ("P5", "加锁：pending−−；为 0 时 fillsz += rsvd"),
            ("P6", "设置「有数据」：唤醒 C、通知 poll；放锁")]),
        (W - 20 - LW, "消费者 C（可能多个）", [
            ("C1", "加锁，查数据：fillsz − 本侧 rsvd ≥ n？"),
            ("C2", "不够：放锁；P 也在等就短读，否则等「有数据」"),
            ("C3", "预留：off = R；R += n；pending++；rsvd += n"),
            ("C4", "放锁，从 buf[off] 读数据"),
            ("C5", "加锁：pending−−；为 0 时 fillsz −= rsvd"),
            ("C6", "设置「有空间」：唤醒 P、通知 poll；放锁")]),
    )
    for x, name, steps in lanes:
        s.rect(x, LY, LW, LH, "#F7F7F7", "#BBBBBB", 1.2, 8)
        s.text(x + LW / 2, LY + 30, name, 15, anchor="middle", bold=True)
        for i, (k, t) in enumerate(steps):
            y = LY + 70 + i * 42
            s.step(x + 24, y - 5, k)
            s.text(x + 44, y, t, 12.5)
    PL, CL = 20 + LW, W - 20 - LW   # inner edges of the lanes

    s.flag(CX - 8, 58)
    s.text(CX + 18, 72, "「有数据」标记", 13, bold=True)
    s.arrow([(PL, 76), (CX - 14, 76)])
    s.text((PL + CX) / 2, 68, "P6 设置", 12, "#333333", "middle")
    s.arrow([(CX, 88), (CL - 2, 88)])
    s.text((CX + CL) / 2 + 30, 82, "唤醒在此等的 C2", 12, "#333333", "middle")

    s.lock(CX, 140, "#333333")
    s.text(CX, 172, "环锁：保护 W/R、预留、fillsz", 12.5, anchor="middle", bold=True)
    s.line(PL + 2, 142, CX - 14, 142, "#333333", 1.4, "5 3")
    s.line(CX + 14, 142, CL - 2, 142, "#333333", 1.4, "5 3")
    s.text((PL + CX) / 2, 134, "P1/P3/P5/P6 持有", 12, "#333333", "middle")
    s.text((CX + CL) / 2, 134, "C1/C3/C5/C6 持有", 12, "#333333", "middle")

    N, CW, BH, BY = 16, 22, 28, 236
    BX = CX - N * CW / 2
    RI, WI = 4, 11
    for i in range(N):
        s.rect(BX + i * CW, BY, CW, BH, "#C9C9C9" if RI <= i < WI else "#FFFFFF", "#777777", 1)
    for idx, lab, acc in ((WI, "W", "只有 P 用：P3 推进"), (RI, "R", "只有 C 用：C3 推进")):
        x = BX + idx * CW
        s.line(x, 210, x, BY - 8, "#333333", 1.8)
        s.head(x, BY - 1, math.pi / 2, "#333333", 8)
        s.text(x, 206, lab, 15, anchor="middle", bold=True, mono=True)
        s.text(x, 192, acc, 11.5, GRAY, "middle")
    fx0, fx1 = BX + RI * CW, BX + WI * CW
    s.line(fx0, BY + BH + 7, fx1, BY + BH + 7, "#333333", 1.4)
    s.line(fx0, BY + BH + 2, fx0, BY + BH + 12, "#333333", 1.4)
    s.line(fx1, BY + BH + 2, fx1, BY + BH + 12, "#333333", 1.4)
    s.text((fx0 + fx1) / 2, BY + BH + 27, "fillsz：P5 加、C5 减；P1、C1 读", 12, "#333333", "middle", bold=True)
    s.arrow([(PL, BY + 14), (BX - 2, BY + 14)], "#333333", 1.8)
    s.text((PL + BX) / 2, BY + 6, "P4 写", 12, "#333333", "middle", bold=True)
    s.arrow([(BX + N * CW + 2, BY + 14), (CL - 2, BY + 14)], "#333333", 1.8)
    s.text((BX + N * CW + CL) / 2, BY + 6, "C4 读", 12, "#333333", "middle", bold=True)
    s.rect(PL + 16, 300, 190, 24, "#FFFFFF", "#777777", 1, 12)
    s.text(PL + 111, 316.5, "P 侧预留：pending · rsvd", 11.5, INK, "middle")
    s.rect(CL - 206, 300, 190, 24, "#FFFFFF", "#777777", 1, 12)
    s.text(CL - 111, 316.5, "C 侧预留：pending · rsvd", 11.5, INK, "middle")

    s.flag(CX - 8, 322)
    s.text(CX + 18, 336, "「有空间」标记", 13, bold=True)
    s.arrow([(CL, 340), (CX + 14, 340)])
    s.text((CX + CL) / 2 + 30, 358, "C6 设置", 12, "#333333", "middle")
    s.arrow([(CX - 8, 352), (PL + 2, 352)])
    s.text((PL + CX) / 2, 346, "唤醒在此等的 P2", 12, "#333333", "middle")
    s.text(CX, 396, "同一套流程有两个方向（xbuf A 向：P = oob、C = ib；B 向相反）。"
           "proxy：已去掉 proxy_write / proxy_read，只剩 oob 调用者；转发者不拿环锁，用自己的 mutex 串行，边读写边发布 fillsz、不预留。", 12.5, GRAY, "middle")

    # ---------------------------------------------------------- part 2
    TY = 436
    s.text(20, TY, "② 每个标记谁写、谁读；代入 Z / z", 17, bold=True)
    cw = (W - 40 - 410) / 4
    cols = [(20, 120), (140, 140), (280, 150)] + [(430 + i * cw, cw) for i in range(4)]
    HY, RH = TY + 14, 36
    table_head(s, cols, HY, ("标记", "P 侧", "C 侧"))
    NA = ("na", "无此项")
    rows = [
        ("W", "P3 推进", "—", ("n", "只有 Z 用"), ("n", "只有 z 用"), ("n", "只有 Z 用"), ("n", "只有 z 用（转发者）")),
        ("R", "—", "C3 推进", ("n", "只有 z 用"), ("n", "只有 Z 用"), ("n", "只有 z 用（转发者）"), ("n", "只有 Z 用")),
        ("fillsz", "P5 加；P1 读", "C5 减；C1 读", ("a", "Z、z 都写"), ("a", "Z、z 都写"),
         ("a", "Z 加 · z 减"), ("a", "z 加 · Z 减")),
        ("P 侧预留", "P3/P5 改", "—", ("n", "只有 Z 用"), ("n", "只有 z 用"), ("n", "只有 Z 用"), ("na", "无：转发者不预留")),
        ("C 侧预留", "—", "C3/C5 改", ("n", "只有 z 用"), ("n", "只有 Z 用"), ("na", "无：转发者不预留"), ("n", "只有 Z 用")),
        ("环锁", "P1/3/5/6 持有", "C1/3/5/6 持有", ("a", "Z、z 都持有"), ("a", "Z、z 都持有"),
         ("n", "只有 Z 用：转发者不拿"), ("n", "只有 Z 用：转发者不拿")),
        ("「有数据」", "P6 设置", "C2 在此等", ("v", "Z 设置 · z 在等"), ("a", "z 设置 · Z 在等"),
         ("v", "Z 设置 · z 转发者处理"), ("a", "z 设置 · Z 在等")),
        ("「有空间」", "P2 在此等", "C6 设置", ("a", "z 设置 · Z 在等"), ("v", "Z 设置 · z 在等"),
         ("a", "z 设置 · Z 在等"), ("na", "无：改用请求量")),
        ("「P 在等」", "—", "C2 读", ("a", "z 拿 Z 的锁读 Z 的等待者"), ("a", "Z 读 z 的等待队列"), NA, NA),
        ("请求量 reqsz", "转发者读、减", "C 加", NA, NA, NA, ("a", "Z 加 · z 减")),
        ("buf 内容", "P4 写", "C4 读", ("n", "Z 写 · z 读"), ("a", "z 写 · Z 读"), ("n", "Z 写 · z 读"), ("a", "z 写 · Z 读")),
        ("非阻塞标志", "调用者读 filp->f_flags", None, ("a", "z 可 fcntl 改 · Z 读"), ("a", "z 可 fcntl 改 · Z 读"),
         ("a", "z 可 fcntl 改 · Z 读"), ("a", "z 可 fcntl 改 · Z 读")),
        ("fd → 对象", "调用者经 fd 表找对象", None, ("a", "z 可 dup2/close 改 · Z 查"), ("a", "z 可 dup2/close 改 · Z 查"),
         ("a", "z 可 dup2/close 改 · Z 查"), ("a", "z 可 dup2/close 改 · Z 查")),
        ("错误 / EOF", "转发者写", "C 读、返回", NA, NA, ("na", "无：写错误直接丢弃"), ("a", "z 写 · Z 返回")),
    ]
    y0 = HY + 50
    for i, (mark, pa, ca, *vs) in enumerate(rows):
        y = y0 + i * RH
        if i == 11:
            s.line(20, y, W - 20, y, "#999999", 1.2)
        elif i:
            s.line(20, y, W - 20, y, "#E3E3E3", 1)
        cy = y + RH / 2 + 4.5
        s.text(cols[0][0] + 10, cy, mark, 13.5, bold=True)
        if ca is None:
            s.text(cols[1][0] + 10, cy, pa, 12, "#333333")
        else:
            s.text(cols[1][0] + 10, cy, pa, 12, "#333333")
            s.text(cols[2][0] + 10, cy, ca, 12, "#333333")
        for (cx, w), (k, lab) in zip(cols[3:], vs):
            s.chip(cx + 6, y + 5, w - 12, RH - 10, k, lab, 12)
    ye = y0 + len(rows) * RH
    s.line(20, ye, W - 20, ye, "#999999", 1.2)
    s.legend(ye + 36, (("a", "交集：Z 受 z 影响（z 写的值、z 持的锁）"), ("v", "交集：Z 要去通知 z"),
                       ("n", "不在交集：只有一个域用，或 Z 写、z 只读"), ("na", "无此项")),
             "Z 可信域（实时侧）  z 不可信域（非实时侧）")
    s.save("fig6-pipe-marks.svg")


# ====================================================================== fig7
def fig7():
    W, H = 1500, 1130
    s = Svg(W, H, "截断之后：xbuf B 向示意，以及每个标记的截断方法与结果")
    CX = W / 2
    s.text(20, 30, "① 截断后的 xbuf B 向（ib 写者 → oob 读者）；A 向对称", 17, bold=True)

    BY0, BH0, BW = 54, 330, 430
    ZL, ZR = 20, W - 20 - BW
    # z side
    s.rect(ZL, BY0, BW, BH0, RED_F, RED, 1.5, 8)
    s.ztext(ZL + 16, BY0 + 28, "z 端：ib 写者（可能多个）", 15, bold=True, base=RED)
    s.lock(ZL + 30, BY0 + 58, RED)
    s.text(ZL + 50, BY0 + 64, "z 锁（spinlock_t）：只管 z 侧的 W 和预留", 12.5, INK)
    s.rect(ZL + 16, BY0 + 80, BW - 32, 26, "#FFFFFF", RED, 1, 13)
    s.ztext(ZL + BW / 2, BY0 + 98, "z 私有：W · pending · rsvd", 12, "middle", bold=True)
    zsteps = [("预留（持 z 锁）：fill_pub + rsvd + n ≤ bufsz？", "off = W; W += n; pending++; rsvd += n"),
              ("拷贝（不持任何锁）", "copy_from_user(buf + off, …)"),
              ("提交（持 z 锁）：pending 为 0 才报告", "pending--; if (!pending) commit(h, rsvd)"),
              ("放不下就等自己的等待队列", "wait_event(ib_wait, …)")]
    for i, (a, b) in enumerate(zsteps):
        y = BY0 + 136 + i * 48
        s.step(ZL + 28, y - 5, f"z{i+1}")
        s.text(ZL + 46, y, a, 12.5, bold=True)
        s.text(ZL + 46, y + 18, b, 11.5, "#333333", mono=True)

    # Z side
    s.rect(ZR, BY0, BW, BH0, BLUE_F, BLUE, 1.5, 8)
    s.ztext(ZR + 16, BY0 + 28, "Z 端：oob 读者", 15, bold=True, base=BLUE)
    s.lock(ZR + 30, BY0 + 58, BLUE)
    s.text(ZR + 50, BY0 + 64, "Z 锁：只在可信代码里，持锁时间有界", 12.5, INK)
    s.rect(ZR + 16, BY0 + 80, BW - 32, 26, "#FFFFFF", BLUE, 1, 13)
    s.ztext(ZR + BW / 2, BY0 + 98, "Z 私有：fill′ · R · pending · rsvd", 12, "middle", bold=True)
    Zsteps = [("查数据：fill′ − rsvd ≥ n？", "（只看自己的 fill′）"),
              ("不够：带超时等，超时告警", "evl_wait_timeout(i_event)"),
              ("预留并读数据，地址只用自己的 R", "off = R; R += n; raw_copy_to_user(…)"),
              ("pending 为 0 时：扣减、发布、记录通知", "fill′ −= rsvd; fill_pub = fill′; notify_post()")]
    for i, (a, b) in enumerate(Zsteps):
        y = BY0 + 136 + i * 48
        s.step(ZR + 28, y - 5, f"Z{i+1}")
        s.text(ZR + 46, y, a, 12.5, bold=True)
        s.text(ZR + 46, y + 18, b, 11.5, "#333333", mono=True)

    L, R = ZL + BW, ZR
    gy = 112
    s.gate(CX, gy, "#333333")
    s.arrow([(L, gy), (CX - 22, gy)], RED, 1.8)
    s.arrow([(CX + 22, gy), (R - 2, gy)], BLUE, 1.8)
    s.text(CX, gy - 32, "② 门 commit(h, n)：z 只传入一个数", 13, anchor="middle", bold=True)
    s.ztext(CX, gy + 36, "Z：n 截断到 bufsz − fill′；fill′ += n；有读者在等才唤醒", 12, "middle")

    N, CW, BH, BYb = 14, 24, 28, 196
    BX = CX - N * CW / 2
    for i in range(N):
        s.rect(BX + i * CW, BYb, CW, BH, "#C9C9C9" if 3 <= i < 9 else "#FFFFFF", "#777777", 1)
    s.arrow([(L, BYb + 14), (BX - 2, BYb + 14)], RED, 1.8)
    s.text((L + BX) / 2, BYb + 6, "z2 写", 11.5, RED, "middle", bold=True)
    s.arrow([(BX + N * CW + 2, BYb + 14), (R - 2, BYb + 14)], BLUE, 1.8)
    s.text((BX + N * CW + R) / 2, BYb + 6, "Z3 读", 11.5, BLUE, "middle", bold=True)
    s.text(CX, BYb + BH + 22, "① 共享 buf：内容只当数据", 13, anchor="middle", bold=True)

    ny = 300
    s.records(CX, ny, "#7A5C99")
    s.arrow([(R - 2, ny), (CX + 30, ny)], BLUE, 1.8)
    s.arrow([(CX - 30, ny), (L + 2, ny)], RED, 1.8)
    s.text(CX, ny - 22, "③ 通知记录：Z 只记录", 13, anchor="middle", bold=True)
    s.ztext(CX, ny + 30, "合成中断；z 取走后自己 wake_up(ib_wait)", 12, "middle")

    py = 364
    s.arrow([(R - 2, py), (L + 2, py)], GRAY, 1.3, dash="5 3")
    s.ztext(CX, py - 7, "④ 发布 fill_pub：z 只读，用来算空闲；Z 不回读", 12, "middle", base=GRAY)

    # ---------------------------------------------------------- part 2
    TY = 430
    s.text(20, TY, "② 每个标记的截断方法与结果（颜色 = 方法）", 17, bold=True)
    s.text(W - 20, TY, "proxy 已去掉 proxy_write / proxy_read", 12.5, GRAY, "end")
    cw = (W - 40 - 120) / 4
    cols = [(20, 120)] + [(140 + i * cw, cw) for i in range(4)]
    HY, RH = TY + 14, 36
    table_head(s, cols, HY, ("标记",))
    NA = ("na", "无此项")
    rows = [
        ("W", ("ok", "Z 独用"), ("ok", "z 独用，z 锁"),
         ("ok", "Z 独用"), ("ok", "z 独用（转发者）")),
        ("R", ("ok", "z 独用，z 锁"), ("ok", "Z 独用"),
         ("ok", "z 独用（转发者）"), ("ok", "Z 独用")),
        ("fillsz", ("cpin", "Z 存 fill′；z 报告读走量，截断后扣减"), ("cpin", "Z 存 fill′；z 报告写入量，截断后计入"),
         ("cpin", "Z 存 fill′；转发者报告读走量"), ("cpin", "Z 存 fill′；转发者报告读入量")),
        ("P 侧预留", ("ok", "Z 独用，Z 锁"), ("ok", "z 独用；pending 为 0 才报告"),
         ("ok", "Z 独用，Z 锁"), NA),
        ("C 侧预留", ("ok", "z 独用；pending 为 0 才报告"), ("ok", "Z 独用，Z 锁"),
         NA, ("ok", "Z 独用，Z 锁")),
        ("环锁", ("ow", "拆成 Z 锁、z 锁，各管本侧"), ("ow", "拆成 Z 锁、z 锁，各管本侧"),
         ("ok", "原本只有 Z 用"), ("ok", "原本只有 Z 用")),
        ("「有数据」", ("rc", "Z 记录，z 取走后自己唤醒"), ("in", "z 调门；Z 按 fill′ 决定唤醒，带超时"),
         ("rc", "Z 记录，转发者取走后处理"), ("in", "转发者调门；Z 按 fill′ 唤醒，带超时")),
        ("「有空间」", ("el", "Z 从不等：积压超阈值告警"), ("rc", "Z 记录，z 取走后自己唤醒"),
         ("el", "oob 写者从不等：积压告警"), NA),
        ("「P 在等」", ("el", "Z 从不等，不再需要"), ("in", "z 经门置提示，Z 只当提示"), NA, NA),
        ("请求量 reqsz", NA, NA, NA, ("cpin", "Z 存请求量、记录通知；转发者报告完成量")),
        ("buf 内容", ("ok", "Z 写，z 读"), ("in", "内容只当数据，不参与地址和控制"),
         ("ok", "Z 写，z 读"), ("in", "内容只当数据，不参与地址和控制")),
        ("非阻塞标志", ("cp", "可信句柄表记录打开方式"), ("cp", "可信句柄表记录打开方式"),
         ("cp", "可信句柄表记录打开方式"), ("cp", "可信句柄表记录打开方式")),
        ("fd → 对象", ("cp", "可信句柄表代替 fd 表"), ("cp", "可信句柄表代替 fd 表"),
         ("cp", "可信句柄表代替 fd 表"), ("cp", "可信句柄表代替 fd 表")),
        ("错误 / EOF", NA, NA, ("na", "无"), ("in", "截断成合法错误码再返回")),
    ]
    y0 = HY + 50
    for i, (mark, *vs) in enumerate(rows):
        y = y0 + i * RH
        if i == 11:
            s.line(20, y, W - 20, y, "#999999", 1.2)
        elif i:
            s.line(20, y, W - 20, y, "#E3E3E3", 1)
        s.text(30, y + RH / 2 + 4.5, mark, 13.5, bold=True)
        for (cx, w), (k, lab) in zip(cols[1:], vs):
            s.chip(cx + 6, y + 5, w - 12, RH - 10, k, lab, 12)
    ye = y0 + len(rows) * RH
    s.line(20, ye, W - 20, ye, "#999999", 1.2)
    s.legend(ye + 36, (("el", "消除"), ("cp", "私有副本"), ("in", "输入化"), ("cpin", "私有副本 + 输入化"),
                       ("rc", "只记录、不调用"), ("ow", "归属单侧"), ("ok", "原本不在交集"), ("na", "无此项")),
             "Z 可信域（实时侧）  z 不可信域（非实时侧）")
    s.save("fig7-pipe-cut.svg")


fig6()
fig7()
