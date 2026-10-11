/* The chart kit (docs/v2/contracts.md §8.2), ported from docs/v2/charts-b.html.
   Lines 1.5 px, 1 px solid grid, bars <= 16 px, one end dot, figures in Geist Mono, colour = area or
   model only, tooltip on hover or tap. The v1 look (thick lines, gradients, dots on every point,
   donuts) is not available here. */
import { fmtT, hm, pad2 } from "./format.js";

const NS = "http://www.w3.org/2000/svg";

/* ---------- helpers ---------- */
export function S(tag, at, parent) {
  const e = document.createElementNS(NS, tag);
  if (at) for (const k in at) e.setAttribute(k, at[k]);
  if (parent) parent.appendChild(e);
  return e;
}
export function H(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

/* ---------- tooltip ---------- */
function tipEl() {
  let t = document.getElementById("tip");
  if (!t) { t = H("div", null, null, document.body); t.id = "tip"; t.setAttribute("role", "tooltip"); }
  return t;
}
function keyEl(parent, kind, color) {
  if (!kind) return;
  const i = H("i", kind === "line" ? "lkey" : "sw", null, parent);
  i.style.background = color;
}
function moveTip(ev) {
  const tip = tipEl();
  const r = tip.getBoundingClientRect();
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + r.width > window.innerWidth - 8) x = ev.clientX - r.width - 14;
  if (x < 8) x = 8;
  if (y + r.height > window.innerHeight - 8) y = ev.clientY - r.height - 14;
  y = Math.max(8, Math.min(y, window.innerHeight - r.height - 8));
  x = Math.max(8, Math.min(x, window.innerWidth - r.width - 8));
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
let pendingClear = null;
function leaveWith(ev, extra) {
  const f = () => { hideTip(); if (extra) extra(); };
  if (ev && ev.pointerType === "touch") { pendingClear = f; return; }
  f();
}
document.addEventListener("pointerdown", () => {
  if (pendingClear) { const f = pendingClear; pendingClear = null; f(); }
});
export function showTip(ev, title, rows) {
  if (pendingClear) { const pc = pendingClear; pendingClear = null; pc(); }
  const tip = tipEl();
  tip.textContent = "";
  if (title) H("div", "tt", title, tip);
  rows.forEach((r) => {
    const d = H("div", r.wrap ? "tr wrap" : "tr", null, tip);
    keyEl(d, r.key, r.color);
    if (r.val != null) H("span", "tv", r.val, d);
    if (r.name) H("span", "tn", r.name, d);
  });
  tip.style.display = "block";
  moveTip(ev);
}
export function hideTip() { tipEl().style.display = "none"; }
export function bindTip(el, fn, enter, leave) {
  function mv(ev) { const t = fn(ev); if (!t) return; showTip(ev, t.title, t.rows); }
  el.addEventListener("pointerenter", (ev) => { mv(ev); if (enter) enter(); });
  el.addEventListener("pointermove", mv);
  el.addEventListener("pointerleave", (ev) => leaveWith(ev, leave));
}

/* ---------- legends ---------- */
export function legend(host, items, kind) {
  items.forEach((it) => {
    const s = H("span", null, null, host);
    const i = H("i", kind === "line" ? "lkey" : "sw", null, s);
    i.style.background = it.color;
    s.appendChild(document.createTextNode(it.name));
  });
}

/* ---------- mount at real pixel width ---------- */
export function mount(box, draw) {
  let last = -1;
  function run() {
    if (!box.isConnected) { window.removeEventListener("resize", run); return; }
    const w = Math.floor(box.clientWidth);
    if (w <= 0 || w === last) return;
    last = w;
    box.textContent = "";
    hideTip();
    draw(box, w);
  }
  if (window.ResizeObserver) new ResizeObserver(run).observe(box);
  window.addEventListener("resize", run);
  run();
}

/* ---------- shared grid ---------- */
function drawGrid(svg, o, y, x1, x2, bottomY) {
  o.ticks.forEach((t) => {
    const yy = Math.round(y(t)) + 0.5;
    S("line", { x1, x2, y1: yy, y2: yy, class: o.zero === t ? "ax" : "gl" }, svg);
    S("text", { x: x1 - 8, y: yy + 4, "text-anchor": "end", class: "tk" }, svg).textContent = o.fmtTick(t);
  });
  if (o.zero == null) {
    const by = Math.round(bottomY) + 0.5;
    S("line", { x1, x2, y1: by, y2: by, class: "ax" }, svg);
  }
}
function xLabels(svg, labels, xf, every, minStep, h, pw, n, step, fromEnd) {
  let e = every || 1;
  while (e * step < minStep) e++;
  if (fromEnd) {
    for (let j = n - 1; j >= 0; j -= e) S("text", { x: xf(j), y: h - 6, "text-anchor": "middle", class: "tk" }, svg).textContent = labels[j];
    return;
  }
  for (let i = 0; i < n; i += e) S("text", { x: xf(i), y: h - 6, "text-anchor": "middle", class: "tk" }, svg).textContent = labels[i];
}

/* ---------- line chart ---------- */
export function lineChart(box, w, o) {
  const h = o.h, pl = o.padL || 44, pr = o.padR || 16, pt = 10, pb = 24;
  box.style.height = h + "px";
  const svg = S("svg", { width: w, height: h, viewBox: "0 0 " + w + " " + h, class: "chart" }, box);
  const lo = o.domain ? o.domain[0] : o.ticks[0], hi = o.domain ? o.domain[1] : o.ticks[o.ticks.length - 1];
  const ph = h - pt - pb, pw = w - pl - pr, n = o.labels.length, step = n > 1 ? pw / (n - 1) : pw;
  const y = (v) => pt + ((hi - v) / (hi - lo)) * ph;
  const x = (i) => pl + i * step;
  drawGrid(svg, o, y, pl, w - pr, y(lo));
  xLabels(svg, o.labels, x, o.xEvery, o.labelW || 34, h, pw, n, step, true);
  const base = pt + ph;
  const pathOf = (vals) => "M" + vals.map((v, i) => x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" L");
  if (o.wash) {
    const s0 = o.series[0];
    const ad = pathOf(s0.vals) + " L" + x(n - 1).toFixed(1) + " " + base + " L" + x(0).toFixed(1) + " " + base + " Z";
    S("path", { d: ad, style: "fill:" + s0.color + ";fill-opacity:.08;stroke:none" }, svg);
  }
  o.series.forEach((s) => S("path", { d: pathOf(s.vals), class: "ln", style: "stroke:" + s.color }, svg));
  const hair = S("line", { y1: pt, y2: base, x1: 0, x2: 0, class: "hair" }, svg);
  hair.style.display = "none";
  o.series.forEach((s, si) => {
    const last = n - 1;
    S("circle", { cx: x(last), cy: y(s.vals[last]), r: 6, style: "fill:var(--panel)" }, svg);
    S("circle", { cx: x(last), cy: y(s.vals[last]), r: 4, style: "fill:" + s.color }, svg);
    if (o.endLabels && o.endLabels[si]) S("text", { x: x(last) + 12, y: y(s.vals[last]) + 4, class: "el" }, svg).textContent = o.endLabels[si];
  });
  const ov = S("rect", { x: 0, y: 0, width: w, height: h, style: "fill:transparent" }, svg);
  function at(ev) {
    const r = svg.getBoundingClientRect();
    let i = step ? Math.round((ev.clientX - r.left - pl) / step) : 0;
    return Math.max(0, Math.min(n - 1, i));
  }
  function mvl(ev) {
    const i = at(ev), xx = Math.round(x(i)) + 0.5;
    showTip(ev, (o.titles || o.labels)[i], o.series.map((s) => ({ key: "line", color: s.color, val: o.fmtTip(s.vals[i]), name: s.name })));
    hair.setAttribute("x1", xx);
    hair.setAttribute("x2", xx);
    hair.style.display = "";
  }
  ov.addEventListener("pointermove", mvl);
  ov.addEventListener("pointerdown", mvl);
  ov.addEventListener("pointerleave", (ev) => leaveWith(ev, () => { hair.style.display = "none"; }));
}

/* ---------- column chart ---------- */
function barPath(x, yt, w, yb, r, up) {
  const hh = yb - yt;
  if (hh <= 0) return "";
  r = Math.max(0, Math.min(r, hh, w / 2));
  if (up) return "M" + x + " " + yb + "V" + (yt + r) + "Q" + x + " " + yt + " " + (x + r) + " " + yt + "H" + (x + w - r) + "Q" + (x + w) + " " + yt + " " + (x + w) + " " + (yt + r) + "V" + yb + "Z";
  return "M" + x + " " + yt + "V" + (yb - r) + "Q" + x + " " + yb + " " + (x + r) + " " + yb + "H" + (x + w - r) + "Q" + (x + w) + " " + yb + " " + (x + w) + " " + (yb - r) + "V" + yt + "Z";
}
export function columnChart(box, w, o) {
  const h = o.h, pl = o.padL || 44, pr = 12, pt = o.padT || 16, pb = 24;
  box.style.height = h + "px";
  const svg = S("svg", { width: w, height: h, viewBox: "0 0 " + w + " " + h, class: "chart" }, box);
  const lo = o.domain ? o.domain[0] : o.ticks[0], hi = o.domain ? o.domain[1] : o.ticks[o.ticks.length - 1];
  const ph = h - pt - pb, pw = w - pl - pr, n = o.cats.length, slot = pw / n;
  const y = (v) => pt + ((hi - v) / (hi - lo)) * ph;
  o.zero = 0;
  drawGrid(svg, o, y, pl, w - pr, y(lo));
  const cx = (i) => pl + slot * (i + 0.5);
  xLabels(svg, o.xlabels, cx, o.xEvery, o.labelW || 40, h, pw, n, slot);
  const y0 = y(0);
  const hits = [], scrub = [];
  const addHit = (xc, wd, g, fn) => hits.push({ xc, wd, g, fn });
  const mode = o.mode;
  for (let i = 0; i < n; i++) {
    if (mode === "stack") {
      const bw = Math.min(16, slot * 0.6), x0 = cx(i) - bw / 2;
      let cum = 0;
      const g = S("g", { class: "mk" }, svg);
      let lastNZ = -1;
      o.series.forEach((s, si) => { if (s.vals[i] > 0) lastNZ = si; });
      o.series.forEach((s, si) => {
        const v = s.vals[i];
        if (v <= 0) return;
        let yb = y(cum), yt = y(cum + v);
        cum += v;
        if (si > 0) yb -= 2;
        if (yb - yt < 3) yt = yb - 3;
        S("path", { d: barPath(x0, yt, bw, yb, si === lastNZ ? 2 : 0, true), style: "fill:" + s.color }, g);
      });
      const total = cum;
      scrub[i] = { g, fn() {
        const rows = o.series.map((s) => ({ key: "sq", color: s.color, val: o.fmtTip(s.vals[i]), name: s.name }));
        rows.push({ val: o.fmtTip(total), name: "Total" });
        return { title: o.titles[i], rows };
      } };
    } else if (mode === "pair") {
      const pw2 = Math.min(16, (slot * 0.7 - 2) / 2);
      const gp = S("g", { class: "mk" }, svg);
      o.series.forEach((s, si) => {
        const xs = cx(i) - pw2 - 1 + si * (pw2 + 2);
        S("path", { d: barPath(xs, y(s.vals[i]), pw2, y0, 2, true), style: "fill:" + s.color }, gp);
      });
      addHit(cx(i), Math.max(24, slot), gp, () => ({ title: o.titles[i], rows: o.series.map((s) => ({ key: "sq", color: s.color, val: o.fmtTip(s.vals[i]), name: s.name })) }));
    } else {
      const s = o.series[0], v = s.vals[i], bw2 = Math.min(16, slot * 0.6), xb = cx(i) - bw2 / 2;
      const g2 = S("g", { class: "mk" }, svg);
      let col = s.color;
      if (mode === "diverge") col = v >= 0 ? "var(--pos)" : "var(--neg)";
      if (v >= 0) S("path", { d: barPath(xb, y(v), bw2, y0, 2, true), style: "fill:" + col }, g2);
      else S("path", { d: barPath(xb, y0, bw2, y(v), 2, false), style: "fill:" + col }, g2);
      let lab = null;
      if (mode === "single") lab = o.fmtCap(v);
      if (mode === "diverge" && (v === o.mx || v === o.mn)) lab = o.fmtCap(v);
      if (lab != null) {
        if (v >= 0) S("text", { x: cx(i), y: y(v) - 6, "text-anchor": "middle", class: "cap" }, svg).textContent = lab;
        else S("text", { x: cx(i), y: y(v) + 16, "text-anchor": "middle", class: "cap" }, svg).textContent = lab;
      }
      addHit(cx(i), Math.max(24, slot), g2, () => ({ title: o.titles[i], rows: [{ key: "sq", color: col, val: o.fmtTip(v), name: o.unit || "" }] }));
    }
  }
  hits.forEach((ht) => {
    const r = S("rect", { x: ht.xc - ht.wd / 2, y: pt, width: ht.wd, height: ph, style: "fill:transparent" }, svg);
    bindTip(r, ht.fn, () => ht.g.classList.add("on"), () => ht.g.classList.remove("on"));
  });
  if (scrub.length) {
    let cur = -1;
    const sov = S("rect", { x: pl, y: pt, width: pw, height: ph, style: "fill:transparent" }, svg);
    const clr = () => { if (cur >= 0) scrub[cur].g.classList.remove("on"); cur = -1; };
    const mvs = (ev) => {
      const r = svg.getBoundingClientRect();
      let i = Math.floor((ev.clientX - r.left - pl) / slot);
      i = Math.max(0, Math.min(n - 1, i));
      const t = scrub[i].fn();
      showTip(ev, t.title, t.rows);
      if (cur !== i) { clr(); cur = i; }
      scrub[i].g.classList.add("on");
    };
    sov.addEventListener("pointermove", mvs);
    sov.addEventListener("pointerdown", mvs);
    sov.addEventListener("pointerleave", (ev) => leaveWith(ev, clr));
  }
}

/* ---------- horizontal bar list (HTML) ---------- */
export function hbars(host, items, max) {
  items.forEach((it) => {
    const row = H("div", "hb", null, host);
    const lab = H("div", "hl", null, row);
    H("span", "n", it.label, lab);
    if (it.sub) H("span", "m", it.sub, lab);
    const tr = H("div", "ht", null, row);
    const bar = H("i", null, null, tr);
    bar.style.width = "calc((100% - 64px) * " + (max > 0 ? it.v / max : 0).toFixed(4) + ")";
    bar.style.background = it.color;
    H("b", null, it.text, tr);
    bindTip(row, () => ({ title: it.label + (it.sub ? " · " + it.sub : ""), rows: [{ key: "sq", color: it.color, val: it.text, name: it.tipName || "" }] }));
  });
}

/* ---------- sparkline ---------- */
export function spark(host, vals) {
  const W = 100, Hh = 24;
  if (!vals || !vals.length) return;
  const mn = Math.min(...vals), mx = Math.max(...vals), rg = mx - mn || 1;
  const svg = S("svg", { width: W, height: Hh, viewBox: "0 0 " + W + " " + Hh }, host);
  const px = (i) => (vals.length > 1 ? 6 + (i * (W - 12)) / (vals.length - 1) : W / 2);
  const py = (v) => 6 + ((mx - v) / rg) * (Hh - 12);
  S("path", { d: "M" + vals.map((v, i) => px(i).toFixed(1) + " " + py(v).toFixed(1)).join(" L"), class: "ln", style: "stroke:var(--ctx-1)" }, svg);
  const l = vals.length - 1;
  S("circle", { cx: px(l), cy: py(vals[l]), r: 6, style: "fill:var(--panel)" }, svg);
  S("circle", { cx: px(l), cy: py(vals[l]), r: 4, style: "fill:var(--data)" }, svg);
}

/* ---------- tables ---------- */
export function table(host, heads, rows) {
  const t = H("table", "tbl", null, host);
  const tr = H("tr", null, null, H("thead", null, null, t));
  heads.forEach((hd) => H("th", hd.num ? "num" : null, hd.t, tr));
  const tb = H("tbody", null, null, t);
  rows.forEach((r) => {
    const row = H("tr", null, null, tb);
    r.forEach((c, i) => {
      const td = H("td", ((heads[i].num ? "num" : "") + (heads[i].mu ? " mu" : "")).trim() || null, null, row);
      if (typeof c === "function") c(td);
      else td.textContent = c;
    });
  });
  return t;
}
export function meterCell(pct) {
  return (td) => {
    const mc = H("span", "mc", null, td);
    const m = H("span", "mt", null, mc);
    m.style.setProperty("--c", "var(--data)");
    const i = H("i", null, null, m);
    i.style.width = pct + "%";
    H("span", null, pct + " %", mc);
  };
}
export function areaCell(areaId, areaName) {
  return (td) => {
    const a = H("span", "ar", null, td);
    const s = H("i", "sw", null, a);
    s.style.background = areaId ? "var(--a-" + areaId + ")" : "var(--ctx-2)";
    H("span", null, areaName || "Unassigned", a);
  };
}
export function verdictCell(glyph, color, label) {
  return (td) => {
    const g = H("span", "gl2", glyph, td);
    g.style.color = color;
    td.appendChild(document.createTextNode(label));
  };
}

/* ---------- progress meter ---------- */
export function progress(host, o) {
  host.classList.add("pm");
  const row = H("div", "row", null, host);
  H("span", null, o.label, row);
  H("span", "mono", o.value_text, row);
  const m = H("div", "mt", null, host);
  m.style.setProperty("--c", o.color || "var(--data)");
  const i = H("i", null, null, m);
  i.style.width = Math.max(0, Math.min(100, o.pct || 0)) + "%";
  if (o.caption) H("div", "cap2", o.caption, host);
}

/* ---------- stacked bar with legend grid ("time by area") ---------- */
export function stackBar(host, parts, fmt) {
  const tot = parts.reduce((a, d) => a + d.value, 0) || 1;
  const stk = H("div", "stk", null, host);
  const alg = H("div", "alg", null, host);
  parts.forEach((d) => {
    const pct = Math.round((d.value / tot) * 100);
    const hit = H("span", "hit", null, stk);
    hit.style.flex = Math.max(d.value, 0.0001) + " 1 0";
    const bar = H("i", null, null, hit);
    bar.style.background = d.color;
    bindTip(hit, () => ({ title: d.name, rows: [{ key: "sq", color: d.color, val: fmt(d.value), name: pct + " %" }] }));
    const sw = H("i", "sw", null, alg);
    sw.style.background = d.color;
    H("span", "a", d.name, alg);
    H("span", "n", fmt(d.value), alg);
    H("span", "p", pct + " %", alg);
  });
}

/* ---------- timelines (Plan lanes, Team rows) ---------- */
function lum(rgb) {
  const c = rgb.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}
function cssRgb(value) {
  const probe = H("span", null, null, document.body);
  probe.style.color = value;
  probe.style.display = "none";
  const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(getComputedStyle(probe).color);
  probe.remove();
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}
/** The text colour (ink or bg) that reads better on a filled block of `color`. */
export function onColor(color) {
  const c = cssRgb(color), ink = cssRgb("var(--ink)"), bg = cssRgb("var(--bg)");
  if (!c || !ink || !bg) return "var(--ink)";
  const L = lum(c), Li = lum(ink), Lb = lum(bg);
  return (Li + 0.05) / (L + 0.05) >= (L + 0.05) / (Lb + 0.05) ? "var(--ink)" : "var(--bg)";
}
function blockColor(b) { return b.color || (b.area ? "var(--a-" + b.area + ")" : "var(--ctx-1)"); }

/**
 * timelineLanes(host, {start_h, end_h, lanes: [{label, right?, blocks: [{start, end, area|color, title, sub, kind}]}],
 *                      now?, variant?: 'lanes' | 'rows', labelHead?})
 * start/end/now are "HH:MM" strings. 'lanes' (default) is the Plan layout; 'rows' is the Team layout
 * (thin bars, a right-hand column per row).
 */
export function timelineLanes(host, o) {
  const s0 = o.start_h, e0 = o.end_h, span = Math.max(1, e0 - s0);
  const pctOf = (hr) => Math.max(0, Math.min(100, ((hr - s0) / span) * 100));
  const nHours = Math.max(1, Math.ceil(span));
  const colsStyle = "grid-template-columns:repeat(" + nHours + ",1fr)";
  const gridSize = "calc(100%/" + nHours + ") 100%";
  function axis(parent) {
    const ax = H("div", "axis", null, parent);
    ax.style.cssText = colsStyle;
    for (let hr = Math.floor(s0); hr < Math.floor(s0) + nHours; hr++) H("span", null, pad2(hr % 24), ax);
    return ax;
  }
  const nowH = o.now ? hm(o.now) : null;
  const showNow = nowH != null && nowH >= s0 && nowH <= e0;

  if (o.variant === "rows") {
    const wrap = H("div", null, null, host);
    const hd = H("div", "tmr hd", null, wrap);
    H("div", null, null, hd);
    axis(hd);
    H("div", null, null, hd);
    o.lanes.forEach((ln) => {
      const row = H("div", "tmr", null, wrap);
      const l = H("div", "rowline", null, row);
      const nm = H("div", "tn2", ln.label, l);
      nm.title = ln.label;
      const lane = H("div", "tmlane", null, row);
      lane.style.backgroundSize = gridSize;
      const u = H("div", "rowline", null, row);
      u.style.justifyContent = "flex-end";
      H("div", "tu", ln.right || "", u);
      ln.blocks.forEach((b) => {
        const a = hm(b.start), z = hm(b.end);
        if (a == null || z == null) return;
        const seg = H("div", "tseg", null, lane);
        seg.style.left = pctOf(a).toFixed(2) + "%";
        seg.style.width = Math.max(0, pctOf(z) - pctOf(a)).toFixed(2) + "%";
        const i = H("i", null, null, seg);
        i.style.background = blockColor(b);
        bindTip(seg, () => ({ title: b.title, rows: [{ key: "sq", color: blockColor(b), val: b.sub || "" }, { val: b.start + "–" + b.end }] }));
      });
    });
    return;
  }

  const scroll = H("div", "tscroll", null, host);
  const tl = H("div", "tl", null, scroll);
  const left = H("div", "tl-l", null, tl);
  H("div", "spacer", null, left);
  const mid = H("div", "tl-m", null, tl);
  axis(mid);
  o.lanes.forEach((ln) => {
    const rl = H("div", "rl", null, left);
    if (ln.strong) H("b", null, ln.label, rl); else rl.textContent = ln.label;
    const lane = H("div", "lane", null, mid);
    lane.style.backgroundSize = gridSize;
    ln.blocks.forEach((b) => {
      const a = hm(b.start), z = hm(b.end);
      if (a == null || z == null) return;
      const kind = b.kind || "mine";
      const tiny = z - a <= 0.5;
      const el = H("div", "blk" + (kind === "agent" ? " ag" : "") + (kind === "review" ? " rv" : "") + (tiny ? " tiny" : ""), null, lane);
      el.style.left = pctOf(a).toFixed(2) + "%";
      el.style.width = Math.max(0, pctOf(z) - pctOf(a)).toFixed(2) + "%";
      const col = blockColor(b);
      const span2 = b.start + "–" + b.end;
      if (kind === "agent") { el.style.borderColor = col; el.style.background = "color-mix(in srgb, " + col + " 12%, transparent)"; }
      else if (kind === "review") { el.style.borderColor = col; el.style.borderStyle = "solid"; }
      else { el.style.background = col; el.style.color = onColor(col); }
      H("div", "bt", b.title, el);
      H("div", "bs", kind === "agent" ? (b.sub || "") : (z - a <= 1.25 ? b.start : span2), el);
      bindTip(el, () => ({ title: b.title, rows: [{ key: "sq", color: col, val: b.areaName || b.area || "" }, { val: span2, name: kind === "agent" ? (b.sub || "") : kind === "review" ? "review" : (b.sub || "") }] }));
    });
  });
  H("div", "nowrow", null, left);
  H("div", "nowrow", null, mid);
  if (showNow) {
    const p = pctOf(nowH).toFixed(2) + "%";
    const n1 = H("div", "now", null, mid); n1.style.left = p;
    const n2 = H("div", "nowl", o.now, mid); n2.style.left = p;
  }
  function fitTiny() {
    host.querySelectorAll(".blk.tiny").forEach((b) => {
      b.classList.remove("nt");
      const t = b.querySelector(".bt");
      if (t.scrollWidth > t.clientWidth) b.classList.add("nt");
    });
  }
  fitTiny();
  if (window.ResizeObserver) new ResizeObserver(fitTiny).observe(mid);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(fitTiny);
}

export { fmtT };
