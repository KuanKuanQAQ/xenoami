// Group-meeting slides: EVL on LKL -> closures and cuts -> xbuf/proxy.
// Each figure is defined once (inches, figure-local) and rendered both to
// an SVG file and to editable PowerPoint shapes.
const fs = require("fs");
const path = require("path");
const pptxgen = require("pptxgenjs");
const { applyTheme } = require(process.env.PPTX_SKILL + "/scripts/apply_theme.js");

const OUT = process.argv[2];
fs.mkdirSync(OUT, { recursive: true });

const FONT = "Microsoft YaHei";
const HEX = {
  text: "222222", muted: "666666", line: "999999",
  tFill: "DDE8F3", tLine: "2B5F8E",        // trusted
  uFill: "F5E1DC", uLine: "9C3B2B",        // untrusted
  sFill: "EEEEEE", sLine: "888888",        // shared / neutral
};
const THEME = {
  name: "Group Meeting", headFontFace: FONT, bodyFontFace: FONT,
  colors: {
    dk1: HEX.text, lt1: "FFFFFF", dk2: "444444", lt2: "F2F2F2",
    accent1: HEX.tLine, accent2: HEX.uLine, accent3: "888888",
    accent4: "5B7F3A", accent5: "7A5C99", accent6: "B07A1E",
    hlink: HEX.tLine, folHlink: "7A5C99",
  },
};

// ---------------------------------------------------------------- figures
// element kinds: rect, ellipse, text, arrow
const fig1 = { w: 5.7, h: 4.95, els: [] };
(() => {
  const E = fig1.els, cx0 = 0.75, cw = 2.42, gap = 0.1, ry0 = 0.45, rh = 1.55;
  E.push({ t: "text", x: cx0, y: 0, w: cw, h: 0.4, text: "在实时域执行", size: 13, bold: true, align: "center" });
  E.push({ t: "text", x: cx0 + cw + gap, y: 0, w: cw, h: 0.4, text: "在非实时域执行", size: 13, bold: true, align: "center" });
  E.push({ t: "text", x: 0, y: ry0, w: 0.7, h: rh, text: "EVL\n代码", size: 13, bold: true, align: "center" });
  E.push({ t: "text", x: 0, y: ry0 + rh + gap, w: 0.7, h: rh, text: "Linux\n代码", size: 13, bold: true, align: "center" });
  const cell = (c, r, trusted, lines, tag) => {
    const x = cx0 + c * (cw + gap), y = ry0 + r * (rh + gap);
    E.push({ t: "rect", x, y, w: cw, h: rh, fill: trusted ? HEX.tFill : HEX.uFill, line: trusted ? HEX.tLine : HEX.uLine });
    E.push({ t: "text", x: x + 0.1, y: y + 0.1, w: cw - 0.2, h: rh - 0.55, text: lines, size: 11, mono: true });
    E.push({ t: "text", x: x + 0.1, y: y + rh - 0.42, w: cw - 0.2, h: 0.32, text: tag, size: 11, bold: true, color: trusted ? HEX.tLine : HEX.uLine });
  };
  cell(0, 0, true, "evl_schedule()\nxbuf_oob_read()", "需要可信");
  cell(1, 0, false, "xbuf_read()\nxbuf_write()\nfactory 的 sysfs show", "不需要可信");
  cell(0, 1, true, "handle_irq_pipelined()\nset_next_event()\nraw_copy_to_user()", "需要可信（信任基）");
  cell(1, 1, false, "kernel_write()\nwake_up()\nVFS", "不可信");
  const dy = ry0 + 2 * rh + gap + 0.2;
  E.push({ t: "rect", x: cx0, y: dy, w: 2 * cw + gap, h: 0.8, fill: HEX.sFill, line: HEX.sLine });
  E.push({ t: "text", x: cx0 + 0.1, y: dy + 0.06, w: 2 * cw + gap - 0.2, h: 0.85, size: 11,
    text: "数据也不按目录划分：\n原 xbuf 环的 fillsz、rdoff 是 EVL 数据，却由 inband 直接写\nEVL 线程状态 oob_state 放在 Linux 的 thread_info 里" });
})();

const fig2 = { w: 6.4, h: 3.3, els: [] };
(() => {
  const E = fig2.els;
  E.push({ t: "ellipse", x: 0, y: 0.15, w: 4.2, h: 3.1, fill: HEX.tFill, line: HEX.tLine, transparency: 35 });
  E.push({ t: "ellipse", x: 2.2, y: 0.15, w: 4.2, h: 3.1, fill: HEX.uFill, line: HEX.uLine, transparency: 35 });
  E.push({ t: "text", x: 0.3, y: 0.0, w: 1.4, h: 0.4, text: "Z 必须可信", size: 13, bold: true, color: HEX.tLine });
  E.push({ t: "text", x: 4.7, y: 0.0, w: 1.5, h: 0.4, text: "z 无法可信", size: 13, bold: true, color: HEX.uLine, align: "right" });
  E.push({ t: "text", x: 0.45, y: 0.6, w: 1.7, h: 2.3, size: 11, align: "center",
    text: "F1：实时域执行的代码\n↓ 依赖\nD1：F1 依赖的数据\n↑ 写\nF2：写 D1 的函数\n↓ 依赖\nD2 …" });
  E.push({ t: "text", x: 4.25, y: 0.6, w: 1.7, h: 2.3, size: 11, align: "center",
    text: "f1：非实时线程可触及\n↓ 写\nd1：f1 能写的数据\n↑ 依赖\nf2：依赖 d1 的函数\n↓ 写\nd2 …" });
  E.push({ t: "text", x: 2.25, y: 0.75, w: 1.9, h: 0.3, text: "Z∩z 冲突集", size: 11, bold: true, align: "center" });
  E.push({ t: "text", x: 2.25, y: 1.1, w: 1.9, h: 1.6, size: 10, align: "center",
    text: "xbuf 环下标\n与 fillsz\n\nEVL 等待对象\n\nproxy 写端\n（两域共用）" });
})();

const fig3 = { w: 12.3, h: 2.45, els: [] };
(() => {
  const E = fig3.els;
  E.push({ t: "rect", x: 0, y: 0.7, w: 1.5, h: 0.9, fill: "FFFFFF", line: HEX.tLine, text: "实时线程", size: 12, bold: true });
  E.push({ t: "arrow", x1: 1.5, y1: 1.15, x2: 2.9, y2: 1.15, both: true });
  E.push({ t: "text", x: 1.45, y: 1.2, w: 1.5, h: 0.6, text: "oob_read/write\n拷贝本线程缓冲区", size: 9, align: "center", color: HEX.muted });
  E.push({ t: "rect", x: 2.9, y: 0.3, w: 3.0, h: 1.7, fill: HEX.tFill, line: HEX.tLine });
  E.push({ t: "text", x: 3.0, y: 0.38, w: 2.8, h: 1.55, size: 11,
    text: [{ text: "可信端（Z）", options: { bold: true, breakLine: true } },
           { text: "本端读写下标、字节计数", options: { breakLine: true } },
           { text: "等待对象（EVL flag）", options: { breakLine: true } },
           { text: "积压告警、超时告警" }] });
  E.push({ t: "rect", x: 6.6, y: 0.7, w: 1.9, h: 0.9, fill: HEX.sFill, line: HEX.sLine, text: "共享 bufmem\n内容不可信", size: 11 });
  E.push({ t: "arrow", x1: 5.9, y1: 1.15, x2: 6.6, y2: 1.15, both: true });
  E.push({ t: "arrow", x1: 8.5, y1: 1.15, x2: 9.2, y2: 1.15, both: true });
  E.push({ t: "rect", x: 9.2, y: 0.3, w: 3.1, h: 1.7, fill: HEX.uFill, line: HEX.uLine });
  E.push({ t: "text", x: 9.3, y: 0.38, w: 2.95, h: 1.55, size: 11,
    text: [{ text: "不可信端（z）", options: { bold: true, breakLine: true } },
           { text: "xbuf：inband read/write", options: { breakLine: true } },
           { text: "proxy：转发者 kernel_read/write", options: { breakLine: true } },
           { text: "→ 目标文件的 f_op", options: { breakLine: true } },
           { text: "（tty 驱动、ext4、pipe…）" }] });
  E.push({ t: "arrow", x1: 9.2, y1: 0.48, x2: 5.9, y2: 0.48 });
  E.push({ t: "text", x: 5.9, y: 0.0, w: 3.3, h: 0.45, text: "报告“读/写了 n 字节”（经门，可信端截断）", size: 10, align: "center" });
  E.push({ t: "arrow", x1: 5.9, y1: 1.82, x2: 9.2, y2: 1.82 });
  E.push({ t: "text", x: 5.9, y: 1.87, w: 3.3, h: 0.45, text: "通知：可信端只记录，z 收到合成中断后来取", size: 10, align: "center" });
})();

// ---------------------------------------------------------------- SVG
const PX = 96;
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
function runsToLines(text) {
  if (typeof text === "string") return text.split("\n").map((l) => [{ s: l, bold: false }]);
  const lines = [[]];
  for (const r of text) {
    lines[lines.length - 1].push({ s: r.text, bold: !!(r.options && r.options.bold) });
    if (r.options && r.options.breakLine) lines.push([]);
  }
  return lines;
}
function svgText(e, x, y, w, h, align, valign) {
  const fs_ = (e.size || 12) * PX / 72, lh = fs_ * 1.3;
  const lines = runsToLines(e.text);
  const total = lines.length * lh;
  let y0 = valign === "middle" ? y + (h - total) / 2 : y;
  const anchor = align === "center" ? "middle" : align === "right" ? "end" : "start";
  const ax = align === "center" ? x + w / 2 : align === "right" ? x + w : x;
  const fam = e.mono ? `'Consolas','Noto Sans Mono CJK SC',monospace` : `'Microsoft YaHei','Noto Sans CJK SC',sans-serif`;
  let out = "";
  lines.forEach((ln, i) => {
    const spans = ln.map((r) => `<tspan font-weight="${r.bold || e.bold ? 700 : 400}">${esc(r.s)}</tspan>`).join("");
    out += `<text x="${ax.toFixed(1)}" y="${(y0 + lh * i + fs_).toFixed(1)}" font-size="${fs_.toFixed(1)}" font-family="${fam}" fill="#${e.color || HEX.text}" text-anchor="${anchor}">${spans}</text>\n`;
  });
  return out;
}
function toSVG(fig) {
  const W = fig.w * PX, H = fig.h * PX;
  let s = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">\n`;
  s += `<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" fill="#555"/></marker></defs>\n`;
  s += `<rect width="${W}" height="${H}" fill="#fff"/>\n`;
  for (const e of fig.els) {
    const x = (e.x || 0) * PX, y = (e.y || 0) * PX, w = (e.w || 0) * PX, h = (e.h || 0) * PX;
    if (e.t === "rect") {
      s += `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="#${e.fill}" stroke="#${e.line}" stroke-width="1.3"/>\n`;
      if (e.text) s += svgText(e, x, y, w, h, "center", "middle");
    } else if (e.t === "ellipse") {
      s += `<ellipse cx="${x + w / 2}" cy="${y + h / 2}" rx="${w / 2}" ry="${h / 2}" fill="#${e.fill}" fill-opacity="${(100 - (e.transparency || 0)) / 100}" stroke="#${e.line}" stroke-width="1.3"/>\n`;
    } else if (e.t === "text") {
      s += svgText(e, x, y, w, h, e.align || "left", "top");
    } else if (e.t === "arrow") {
      s += `<line x1="${e.x1 * PX}" y1="${e.y1 * PX}" x2="${e.x2 * PX}" y2="${e.y2 * PX}" stroke="#555" stroke-width="1.3" marker-end="url(#ah)"${e.both ? ' marker-start="url(#ah)"' : ""}/>\n`;
    }
  }
  return s + "</svg>\n";
}

// ---------------------------------------------------------------- PPT shapes
function drawFig(pres, slide, fig, ox, oy, name) {
  let k = 0;
  for (const e of fig.els) {
    const id = `${name}-${e.t}-${k++}`;
    const base = { x: ox + (e.x || 0), y: oy + (e.y || 0), w: e.w, h: e.h, objectName: id };
    if (e.t === "rect" || e.t === "ellipse") {
      const opt = { ...base, fill: { color: e.fill, transparency: e.transparency || 0 }, line: { color: e.line, width: 1 } };
      if (e.text) {
        slide.addText(e.text, { ...opt, shape: e.t === "rect" ? pres.shapes.RECTANGLE : pres.shapes.OVAL,
          fontSize: e.size || 12, bold: !!e.bold, color: e.color || HEX.text, align: "center", valign: "middle", margin: 2 });
      } else {
        slide.addShape(e.t === "rect" ? pres.shapes.RECTANGLE : pres.shapes.OVAL, opt);
      }
    } else if (e.t === "text") {
      slide.addText(e.text, { ...base, isTextBox: true, fontSize: e.size || 12, bold: !!e.bold,
        color: e.color || HEX.text, align: e.align || "left", valign: "top", margin: 0,
        fontFace: e.mono ? "Consolas" : undefined, paraSpaceAfter: 0 });
    } else if (e.t === "arrow") {
      const x = Math.min(e.x1, e.x2), y = Math.min(e.y1, e.y2);
      slide.addShape(pres.shapes.LINE, { x: ox + x, y: oy + y, w: Math.abs(e.x2 - e.x1), h: Math.abs(e.y2 - e.y1),
        flipH: e.x2 < e.x1, objectName: id,
        line: { color: "555555", width: 1, endArrowType: "triangle", beginArrowType: e.both ? "triangle" : undefined } });
    }
  }
}

// ---------------------------------------------------------------- deck
const NOTES = JSON.parse(fs.readFileSync(path.join(__dirname, "notes.json"), "utf8"));

async function main() {
  // Figures are hand-drawn SVG files in OUT; embed them as SVG pictures
  // (PowerPoint: right click -> Convert to Shape to edit them).
  const svg = (s, n, x, y, w) => {
    const t = fs.readFileSync(path.join(OUT, n + ".svg"), "utf8");
    const W = +t.match(/width="([0-9.]+)"/)[1], Hh = +t.match(/height="([0-9.]+)"/)[1];
    s.addImage({ path: path.join(OUT, n + ".svg"), x, y, w, h: w * Hh / W, objectName: n, altText: n });
  };

  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.theme = { headFontFace: FONT, bodyFontFace: FONT };
  pres.title = "EVL 可信划分：从 LKL 承接到闭包截断";
  const C = pres.SchemeColor;
  pres.defineSlideMaster({
    title: "TITLE_ONLY",
    background: { color: "FFFFFF" },
    objects: [{ placeholder: { options: { name: "title", type: "title", x: 0.5, y: 0.3, w: 12.33, h: 0.65,
      fontSize: 26, bold: true, color: C.text1, align: "left", valign: "middle", margin: 0 }, text: "" } }],
    slideNumber: { x: 12.4, y: 7.0, w: 0.5, h: 0.3, fontSize: 10, color: C.text2 },
  });

  const body = (s, runs, opt) => s.addText(runs, { isTextBox: true, valign: "top", margin: 0, color: C.text1,
    fontSize: 14, paraSpaceAfter: 4, ...opt });
  const H = (t) => ({ text: t, options: { bold: true, breakLine: true, paraSpaceBefore: 8 } });
  const B = (t, last) => ({ text: t, options: { bullet: { indent: 14 }, breakLine: !last, indentLevel: 0 } });
  const P = (t, last, extra) => ({ text: t, options: { breakLine: !last, ...(extra || {}) } });

  // ---- slide 1
  let s = pres.addSlide({ masterName: "TITLE_ONLY" });
  s.addText("把 EVL 对 Linux 的调用换成调用 LKL：两个问题", { placeholder: "title" });
  body(s, [
    P("出发点：可信域不应调用不可信域的函数 → 朴素方案：EVL 调 Linux 改为调 LKL", false, { color: C.text2 }),
    H("① 正确性：EVL 调 Linux，是为了操作 Linux 里的对象"),
    B("xbuf：wake_up() 要唤醒的是睡在 Linux 调度器上的任务"),
    B("proxy：kernel_write(filp) 写的是用户进程在 Linux 里打开的文件"),
    B("把 filp 交给 LKL：执行的代码换成了 LKL，数据仍是 Linux 的 file、inode、锁，f_op 仍指向 Linux 驱动；两套内核的结构布局、锁实现都不同"),
    P("→ 只换了代码，依赖的数据没变，也不再正确", false, { bold: true, color: C.accent2 }),
    H("② 实时性：不可信方影响实时域，不只靠“被调用”"),
    B("数据：原 xbuf 中 oob 与 inband 共用一把 hard_spinlock，inband 持锁直接改 fillsz"),
    B("反向调用：inband 读者直接调用 evl_raise_flag_nosched()，进入 EVL 调度"),
    B("实时路径上本就有 Linux 代码：中断流水线、tick 调用 set_next_event()", true),
  ], { x: 0.5, y: 1.2, w: 6.3, h: 5.7 });
  svg(s, "fig1-concepts", 7.13, 1.2, 5.7);
  body(s, "是否可信，由“在哪个域执行、数据被谁写”决定；不由代码所在目录或调用方向决定",
    { x: 7.13, y: 6.3, w: 5.7, h: 0.6, fontSize: 13, bold: true, color: C.accent1 });
  s.addNotes(NOTES[0]);

  // ---- slide 2
  s = pres.addSlide({ masterName: "TITLE_ONLY" });
  s.addText("回到目标：两个闭包 Z、z 及其截断", { placeholder: "title" });
  body(s, [
    P("保护目标：线程在实时域中运行的正确性与实时性", false, { bold: true }),
    P("Z（必须可信）：实时域执行的代码 F1 及其依赖的数据 D1；写 D1 的函数 F2；F2 依赖的数据 D2；……", false, { paraSpaceBefore: 6 }),
    P("z（无法可信）：纯非实时线程能触及的函数 f1；f1 能写的数据 d1；依赖 d1 的函数 f2；……", false, { paraSpaceBefore: 6 }),
    P("双核架构下二者大量重叠。不截断，Z 会吸收绝大部分 Linux", false, { paraSpaceBefore: 6 }),
    P("截断：逐个消除 Z∩z 中的元素", true, { paraSpaceBefore: 6, bold: true, color: C.accent1 }),
  ], { x: 0.5, y: 1.2, w: 5.6, h: 3.1 });
  svg(s, "fig2-closures", 6.43, 1.1, 6.4);
  const hdr = (t) => ({ text: t, options: { bold: true, fill: { color: "F2F2F2" } } });
  s.addTable([
    [hdr("截断方法"), hdr("做法"), hdr("例子")],
    ["私有副本", "可信方持有自己的一份数据（或函数），不再依赖 z 的那份",
      "可信端私有下标；可信句柄表代替 fd 表；LKL（只适用于状态私有的函数：分配器、数据结构库）"],
    ["输入化", "z 对 Z 的影响只作为输入进入，由可信代码校验后自己修改可信状态。“门”（z 推送）和“共享字 + 校验”（Z 拉取）是同一方法的两种传递方式",
      "门铃；不可信端报告“读/写了 n 字节”，可信端截断后更新自己的下标"],
    ["只记录、不调用", "Z 需要 z 做事时只留下记录，由 z 自己来取、自己执行",
      "通知中继：记录挂在可信链表上，发合成中断，inband 处理函数来取"],
  ], { x: 0.5, y: 4.4, w: 12.33, colW: [1.6, 5.6, 5.13], fontSize: 12, color: C.text1,
    border: { type: "solid", pt: 0.75, color: "BBBBBB" }, valign: "middle", margin: 0.06, objectName: "cut-table" });
  s.addNotes(NOTES[1]);

  // ---- slide 3: original xbuf path
  s = pres.addSlide({ masterName: "TITLE_ONLY" });
  s.addText("原实现：inband 写路径依赖 Linux 对象，却持 oob 锁、改 oob 状态", { placeholder: "title" });
  svg(s, "fig4-xbuf-orig", 0.97, 1.08, 11.4);
  s.addNotes(NOTES[2]);

  // ---- slide 4: trusted-end design
  s = pres.addSlide({ masterName: "TITLE_ONLY" });
  s.addText("改后：可信端自持状态，不可信端只报告结果", { placeholder: "title" });
  svg(s, "fig5-xbuf-new", 0.97, 1.08, 11.4);
  s.addNotes(NOTES[3]);

  // ---- slide 5: why not protect the pipe
  s = pres.addSlide({ masterName: "TITLE_ONLY" });
  s.addText("保护可信端，而不是保护管道", { placeholder: "title" });
  body(s, [
    P("方案一  保护管道：下标归可信域，不可信方经门 reserve / commit", false, { bold: true }),
    P("可信端要等不可信方 commit（nesting 归零）才能发布数据 → 保护管道反而让实时端受对方拖累", false, { color: C.text2 }),
    P("方案二  按端拆分：各端管自己的下标，共享原子计数 fillsz（已写未读的字节数）", false, { bold: true, paraSpaceBefore: 6 }),
    P("不可信方把 fillsz 写成 bufsz 或 0 → 可信写者永远见满，可信读者永远见空", false, { color: C.text2 }),
    P("方案三  可信端自持：可信端独自维护本端状态；不可信端只报告读写结果，可信端截断后自己更新", false, { bold: true, paraSpaceBefore: 6 }),
    P("从不等待对方：积压过多就告警（丢弃或覆盖），过久未被唤醒也告警", true, { color: C.text2 }),
  ], { x: 0.5, y: 1.3, w: 6.1, h: 5.6, fontSize: 15 });
  body(s, [
    P("为什么不必保护管道", false, { bold: true }),
    B("对端就是不可信方，管道内容本就只能当输入"),
    B("篡改元数据的后果（丢、重、旧、乱序、空、满）⊆ 篡改内容的后果"),
    B("内容做不到、元数据才能做到的只有三类，才需截断：越界、时间、控制流"),
    P("前提：管道另一端就是不可信方；两端都可信、只途经不可信内存时须另作完整性保护", false, { color: C.text2, fontSize: 12 }),
    P("遗留：恶意频繁报告 / 按门铃", false, { bold: true, paraSpaceBefore: 6 }),
    P("无法阻止，只能使其无害：只在可信端自己的条件变化时唤醒；“有数据”的次数受环容量和实时读者消费限制；门内工作量有界", true),
  ], { x: 6.9, y: 1.3, w: 5.93, h: 5.6, fontSize: 15 });
  s.addNotes(NOTES[4]);

  const file = path.join(OUT, "slides.pptx");
  await pres.writeFile({ fileName: file });
  await applyTheme(file, THEME);
  console.log("wrote", file);
}
main().catch((e) => { console.error(e); process.exit(1); });
