/* Plan screen: the two timelines (day view: lanes along x; week view: seven day columns along y)
   and the drag interactions on them. Looks follow docs/v2/charts-b.html ("Plan · Thursday"). */
import {
  AXIS_MIN, AXIS_MAX, fromMin, minToFrac, pointToSlot, inside, moveRange, resizeRange,
  layoutBlocks, areaVar, dayTotals, fmtHours, mineBlocks, allDayEvents, weekDays, dayShort, dowOf, recurText, clamp,
} from "./plan-core.js";

const ROW_H = 44;          // one row of the calendar in the day view
const WEEK_H = 660;        // 15 h x 44 px: a one-hour block fits the time line plus a whole title line
const HOURS = (AXIS_MAX - AXIS_MIN) / 60;

function mk(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

function areaCol(P, a) { return areaVar(P.ctx.areas, a); }

/* ---------- entry point ---------- */
export function renderTime(P, host) {
  const { S, ctx } = P;
  host.textContent = "";
  S.surface = null;
  const ch = mk("div", "ch", null, host);
  const ct = mk("div", "ct", null, ch);
  mk("h3", null, S.view === "day" ? "Calendar" : "The week", ct);
  mk("p", null, "07:00 to 22:00. Drag an event to move it, drag an edge to resize it, click it to edit.", ct);
  if (S.view === "day") {
    const t = dayTotals(S.data.events, S.day);
    mk("p", "plan-tot mono", "work " + fmtHours(t.work) + "h · sessions " + fmtHours(t.sessions) + "h", ct);
  }
  const lg = mk("div", "lg", null, ch);
  ctx.kit.legend(lg, ctx.areas.map((a) => ({ name: a.name, color: areaVar(ctx.areas, a.id) })).concat([{ name: "Unassigned", color: "var(--ctx-2)" }]), "sq");
  const body = mk("div", "cb plan-tl", null, host);
  if (S.view === "day") dayView(P, body); else weekView(P, body);
  const leg = mk("div", "pl-leg", null, host);
  mk("span", "sep", "filled = event · tinted = project plan, read-only · outlined = review scheduled · full width = full attention, split = sessions in parallel · line = now · hover or tap for details", leg);
}

/* ---------- blocks ---------- */
function tipFor(P, b) {
  const { ctx } = P;
  const an = b.area ? (ctx.areaName ? ctx.areaName(b.area) : b.area) : "Unassigned";
  const span = b.sTxt + " to " + b.eTxt;
  const rows = [{ key: "sq", color: areaCol(P, b.area), val: an }];
  if (b.kind === "ro") rows.push({ val: span, name: "read-only, from the project plan" });
  else if (b.kind === "review") rows.push({ val: span, name: "review" });
  else rows.push({ val: span, name: b.ev.recur ? recurText(b.ev.recur) : b.ev.task_of ? "planned task" : "event" });
  if (b.kind !== "ro") rows.push({ val: b.full ? "full attention" : "shared, runs in parallel" });
  if (b.ev && b.ev.done) rows.push({ val: "done" });
  if (b.ev && b.ev.notes) rows.push({ val: String(b.ev.notes), wrap: true });
  return { title: b.title, rows };
}

function decorate(P, el, b) {
  const { ctx, S } = P;
  const col = areaCol(P, b.area);
  el.style.setProperty("--blk-c", col);
  if (b.kind === "review") {
    el.classList.add("rv");
    el.style.borderColor = col;
    el.style.borderStyle = "solid";
  } else if (b.kind === "ro") {
    el.classList.add("ro");
    el.style.borderColor = col;
    el.style.background = "color-mix(in srgb, " + col + " 38%, transparent)";
  } else {
    el.style.background = col;
    el.style.color = ctx.kit.onColor(col);
  }
  ctx.kit.bindTip(el, () => (S.drag ? null : tipFor(P, b)));
}

function shortOf(P, b) {
  const a = ((P.S.data && P.S.data.areas) || []).find((x) => x.id === b.area);
  return (a && a.short) || "";
}

/** A block too small for its time and title shows the area tag only (time and title in the tooltip):
 *  a third of a week column, or 30 minutes or less on the day timeline. The tag is cut to three letters. */
function tagOnly(P, b, orient) {
  if (!shortOf(P, b)) return false;
  return orient === "v" ? b.rows >= 3 : b.end - b.start <= 30;
}

/** The text of a block. Tag-only blocks: `SHORT`. Day view: the title. Week view: `SHORT · title`. */
function labelFor(P, b, orient) {
  const mark = b.ev && b.ev.done ? "✓ " : "";   // manual done mark
  const short = shortOf(P, b);
  if (!short) return mark + b.title;
  if (tagOnly(P, b, orient)) return mark + short.slice(0, 3);
  return mark + (orient === "v" ? short + " · " + b.title : b.title);
}

function fillBlock(el, b) {
  el.textContent = "";
  mk("div", "bs", b.sTxt + "–" + b.eTxt, el);      // the time range is always shown (first line); the title is clipped instead
  mk("div", "bt", b.label || b.title, el);
}

function makeBlock(P, parent, b, orient, opts) {
  const el = mk("div", "blk plan-blk" + (b.editable ? " editable" : ""), null, parent);
  b.el = el;
  b.orient = orient;
  b.sTxt = fromMin(b.start);
  b.eTxt = fromMin(b.end);
  b.row = opts.row || 0;
  b.rows = opts.rows || 1;
  b.span = opts.span || 1;
  b.label = labelFor(P, b, orient);
  if (b.end - b.start <= 30) el.classList.add("tiny");
  if (b.ev && b.ev.done) el.classList.add("done");
  if (tagOnly(P, b, orient)) el.classList.add("third");   // the area tag only, time and title in the tooltip
  fillBlock(el, b);
  decorate(P, el, b);
  place(el, orient, b.start, b.end, opts);
  if (b.editable) {
    el.tabIndex = 0;
    mk("i", "plan-h " + (orient === "h" ? "l" : "t"), null, el).dataset.edge = "start";
    mk("i", "plan-h " + (orient === "h" ? "r" : "b"), null, el).dataset.edge = "end";
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") P.openForm({ mode: "edit", ev: b.ev }); });
  }
  el._b = b;
  return el;
}

/** Position by time. h: left/width in % of the lane, row picks top; v: top/height in % of the column, pack splits the width. */
function place(el, orient, start, end, o) {
  o = o || {};
  const a = minToFrac(start) * 100, z = minToFrac(end) * 100;
  if (orient === "h") {
    el.style.left = a.toFixed(3) + "%";
    el.style.width = Math.max(0.3, z - a).toFixed(3) + "%";
    const row = o.row || 0;
    el.style.top = (4 + row * ROW_H) + "px";
    el.style.height = ((o.span || 1) * ROW_H - 8) + "px";       // a full-attention block spans every row
    el.style.bottom = "auto";
  } else {
    el.style.top = "calc(" + a.toFixed(3) + "% + 1px)";          // 1px inset top and bottom: neighbours stay distinguishable
    el.style.height = "calc(" + Math.max(1.2, z - a).toFixed(3) + "% - 2px)";
    const n = o.rows || 1, i = o.row || 0;
    el.style.left = "calc(" + (i / n * 100).toFixed(2) + "% + 1px)";
    el.style.width = "calc(" + (100 / n).toFixed(2) + "% - 2px)";
    el.style.bottom = "auto";
  }
}

function blocksFor(P, day) {
  const bs = mineBlocks(P.S.data, day);
  bs.forEach((b) => { b.editable = b.kind !== "ro"; b.day = day; });
  return bs.filter((b) => b.end > AXIS_MIN && b.start < AXIS_MAX);   // sleep hours are not drawn
}

function nowFrac(P, day) {
  if (day !== P.today()) return null;
  const d = new Date();
  const m = d.getHours() * 60 + d.getMinutes();
  return m >= AXIS_MIN && m <= AXIS_MAX ? { frac: minToFrac(m), text: fromMin(m) } : null;
}

function allDayChips(P, parent, day) {
  const list = allDayEvents(P.S.data, day);
  list.forEach((e) => {
    const col = areaCol(P, e.area);
    const b = mk("button", "plan-chip", e.title, parent);
    b.type = "button";
    b.style.borderColor = col;
    b.addEventListener("click", () => P.openForm({ mode: "edit", ev: e }));
    P.ctx.kit.bindTip(b, () => ({ title: e.title, rows: [{ key: "sq", color: col, val: "All day" }, { val: e.recur ? recurText(e.recur) : dayShort(e.day) }] }));
  });
  return list.length;
}

/* ---------- day view ---------- */
function dayView(P, body) {
  const { S } = P;
  const day = S.day;
  const chipsRow = mk("div", "plan-allday", null, body);
  mk("span", "plan-allday-l", "All day", chipsRow);
  const chipBox = mk("div", "plan-allday-c", null, chipsRow);
  if (!allDayChips(P, chipBox, day)) { chipsRow.hidden = true; }

  const scroll = mk("div", "tscroll", null, body);
  scroll.setAttribute("data-keep-scroll", "");
  const tl = mk("div", "tl", null, scroll);
  const left = mk("div", "tl-l", null, tl);
  mk("div", "spacer", null, left);
  const mid = mk("div", "tl-m", null, tl);
  const ax = mk("div", "axis", null, mid);
  ax.style.gridTemplateColumns = "repeat(" + HOURS + ",1fr)";
  for (let h = AXIS_MIN / 60; h < AXIS_MAX / 60; h++) mk("span", null, (h < 10 ? "0" : "") + h, ax);
  const gridSize = "calc(100%/" + HOURS + ") 100%";

  // Calendar
  const mine = blocksFor(P, day);
  const lay = layoutBlocks(mine);
  const rows = lay.maxTracks;
  const rl = mk("div", "rl", null, left);
  rl.style.height = (rows * ROW_H + 8) + "px";
  mk("b", null, "Events", rl);
  const lane = mk("div", "lane plan-surface", null, mid);
  lane.style.height = (rows * ROW_H + 8) + "px";
  lane.style.backgroundSize = gridSize;
  mine.forEach((b, i) => makeBlock(P, lane, b, "h", lay.pos[i].full ? { row: 0, span: rows } : { row: lay.pos[i].track, span: 1 }));
  if (!mine.length) mk("div", "plan-empty-lane", "Nothing planned.", lane);

  mk("div", "nowrow", null, left);
  mk("div", "nowrow", null, mid);
  const nf = nowFrac(P, day);
  if (nf) {
    const l1 = mk("div", "now", null, mid); l1.style.left = (nf.frac * 100).toFixed(2) + "%";
    const l2 = mk("div", "nowl", nf.text, mid); l2.style.left = (nf.frac * 100).toFixed(2) + "%";
  }
  // keep the surface element for hit-testing (tray drops) and wire the drags
  S.surface = { el: lane, orient: "h", cols: 1, days: [day], colEls: [lane] };
  wireSurface(P, S.surface);
  watchFit(body, body);
}

/** Fit each block's text to whole lines: the time line first, then as many whole title lines as the height allows.
 *  A block too short for time plus one title line goes to a one-row layout (title ellipsised, hidden if it cannot fit).
 *  A block narrower than its time range shows the start only (the tooltip has the range). Reads are batched before
 *  writes, and a re-fit is skipped when the host's blocks and size are unchanged since the last fit. */
const fitSig = new WeakMap();
let fitObs = null, fitRaf = 0;

function fitTiny(host, force) {
  const els = Array.from(host.querySelectorAll(".plan-blk")).filter((el) => el.querySelector(".bt") && el.querySelector(".bs"));
  let sig = host.clientWidth + ":" + els.length;
  els.forEach((el) => { sig += ":" + el.clientWidth + "x" + el.clientHeight + (el._b ? (el._b.label || el._b.title) + el._b.start + el._b.end : ""); });
  if (!force && fitSig.get(host) === sig) return;
  // pass 1 (writes): reset every block to its natural state, full time range
  els.forEach((el) => {
    const bt = el.querySelector(".bt"), bs = el.querySelector(".bs");
    el.classList.remove("nt", "one");
    bt.style.webkitLineClamp = "";
    bt.style.maxHeight = "";
    const tb = el._b;
    if (tb) bs.textContent = tb.sTxt + "–" + tb.eTxt;
  });
  // pass 2 (reads)
  const m = els.map((el) => {
    const bt = el.querySelector(".bt"), bs = el.querySelector(".bs");
    const cs = getComputedStyle(el);
    const padW = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    return {
      el, bt, bs, tb: el._b, tiny: el.classList.contains("tiny"),
      wideRange: bs.scrollWidth > el.clientWidth - padW + 0.5,
      inner: el.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom),
      lh: parseFloat(getComputedStyle(bt).lineHeight) || 16, bsH: bs.offsetHeight,
    };
  });
  // pass 3 (writes): start-only time, line counts, one-row mode
  m.forEach((x) => {
    if (x.tb && (x.wideRange || (x.tiny && x.el.classList.contains("v")))) x.bs.textContent = x.tb.sTxt;   // short week blocks: start time + title
    if (x.tiny) return;
    const room = x.inner - x.bsH;
    if (room < x.lh - 0.5) x.el.classList.add("one");
    else {
      const n = Math.max(1, Math.floor((room - 1) / x.lh));
      x.bt.style.webkitLineClamp = String(n);
      x.bt.style.maxHeight = (n * x.lh) + "px";      // hard stop at a whole line
    }
  });
  // pass 4 (reads), pass 5 (writes): one-row blocks whose title has no room
  const rows = m.filter((x) => !x.el.classList.contains("third") && (x.tiny || x.el.classList.contains("one"))).map((x) => ({ x, w: x.bt.clientWidth }));   // a tag stays, clipped
  const cut = [];
  rows.forEach(({ x, w }) => {
    if (w >= 28) return;
    if (x.tb && x.bs.textContent !== x.tb.sTxt) { x.bs.textContent = x.tb.sTxt; cut.push(x); }   // the title gets the room first
    else x.el.classList.add("nt");
  });
  cut.map((x) => ({ x, w: x.bt.clientWidth })).forEach(({ x, w }) => { if (w < 28) x.el.classList.add("nt"); });
  fitSig.set(host, sig);
}

/** One observer per mounted view: a new render replaces the previous one. Fits now, after layout (rAF) and after fonts load. */
function watchFit(host, target) {
  stopFit();
  fitTiny(host);
  fitRaf = requestAnimationFrame(() => { fitRaf = 0; if (host.isConnected) fitTiny(host); });
  if (window.ResizeObserver) { fitObs = new ResizeObserver(() => { if (host.isConnected) fitTiny(host); }); fitObs.observe(target); }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { if (host.isConnected) fitTiny(host, true); });
}

export function stopFit() {
  if (fitObs) { fitObs.disconnect(); fitObs = null; }
  if (fitRaf) { cancelAnimationFrame(fitRaf); fitRaf = 0; }
}

/* ---------- week view ---------- */
function weekView(P, body) {
  const { S } = P;
  const days = weekDays(S.day);
  const today = P.today();
  const scroll = mk("div", "tscroll", null, body);
  scroll.setAttribute("data-keep-scroll", "");
  const wk = mk("div", "plan-wk", null, scroll);

  const head = mk("div", "plan-wk-row", null, wk);
  mk("div", "plan-wk-gut", null, head);
  days.forEach((d) => {
    const b = mk("button", "plan-wk-dh" + (d === today ? " today" : "") + (d === S.day ? " sel" : ""), null, head);
    b.type = "button";
    mk("span", null, dowOf(d), b);
    mk("b", null, String(Number(d.slice(8))), b);
    b.addEventListener("click", () => P.goToDay(d, "day"));
    b.title = "Open " + dayShort(d) + " in the day view";
  });

  const ad = mk("div", "plan-wk-row plan-wk-ad", null, wk);
  mk("div", "plan-wk-gut", "All day", ad);
  let anyAd = false;
  days.forEach((d) => {
    const c = mk("div", "plan-wk-adc", null, ad);
    if (allDayChips(P, c, d)) anyAd = true;
  });
  ad.hidden = !anyAd;

  const main = mk("div", "plan-wk-row plan-wk-main", null, wk);
  const gut = mk("div", "plan-wk-gut", null, main);
  gut.style.height = WEEK_H + "px";
  for (let h = AXIS_MIN / 60; h < AXIS_MAX / 60; h++) {
    const s = mk("span", null, (h < 10 ? "0" : "") + h, gut);
    s.style.top = (minToFrac(h * 60) * 100).toFixed(3) + "%";
  }
  const surf = mk("div", "plan-wk-body plan-surface", null, main);
  surf.style.height = WEEK_H + "px";
  const colEls = days.map((d) => {
    const c = mk("div", "plan-col" + (d === today ? " today" : ""), null, surf);
    c.dataset.day = d;
    const op = mk("div", "plan-op", null, c);        // the operator area: blocks are placed in % of this box
    const mine = blocksFor(P, d);
    const lay = layoutBlocks(mine);
    mine.forEach((b, i) => {
      const el = makeBlock(P, op, b, "v", { row: lay.pos[i].track, rows: lay.pos[i].tracks });
      b.dayIdx = days.indexOf(d);
      el.classList.add("v");
    });
    const nf = nowFrac(P, d);
    if (nf) { const n = mk("div", "plan-wk-now", null, c); n.style.top = (nf.frac * 100).toFixed(3) + "%"; }
    return op;
  });

  S.surface = { el: surf, orient: "v", cols: 7, days, colEls };
  wireSurface(P, S.surface);
  watchFit(wk, surf);
}

/* ---------- hit testing, shared with the review tray ---------- */
export function surfaceRect(P) {
  const s = P.S.surface;
  if (!s) return null;
  const r = s.el.getBoundingClientRect();
  return { orient: s.orient, left: r.left, top: r.top, width: r.width, height: r.height, cols: s.cols };
}
/** The {day, min} under a screen point, or null when the point is not over the calendar. */
export function slotAt(P, x, y) {
  const surf = surfaceRect(P);
  if (!surf || !inside(surf, x, y)) return null;
  const s = pointToSlot(surf, x, y);
  return { day: P.S.surface.days[s.col], min: s.min, col: s.col };
}

/* ---------- drags ---------- */
function wireSurface(P, sf) {
  const { S } = P;
  sf.el.style.touchAction = "pan-x pan-y";
  sf.el.addEventListener("pointerdown", (e) => {
    if (S.drag || (e.pointerType === "mouse" && e.button !== 0)) return;
    const blk = e.target.closest(".plan-blk");
    if (blk && sf.el.contains(blk)) {
      const b = blk._b;
      if (!b || !b.editable) return;
      const edge = e.target.dataset && e.target.dataset.edge;
      begin(P, sf, e, edge ? "resize-" + edge : "move", b);
    }
  });
}

function posLabel(el, a, z) {
  const bs = el.querySelector(".bs");
  if (bs) bs.textContent = fromMin(a) + "–" + fromMin(z);
}

function begin(P, sf, e, mode, b) {
  const { S, ctx } = P;
  const rect = surfaceRect(P);
  if (!rect) return;
  e.preventDefault();
  ctx.kit.hideTip();
  const start = pointToSlot(rect, e.clientX, e.clientY);
  const D = { mode, b, x0: e.clientX, y0: e.clientY, start, moved: false, result: null };
  S.drag = D;
  const orient = sf.orient;
  b.el.classList.add("dragging");

  const startDayIdx = b.dayIdx || 0;
  const onMove = (ev) => {
    const moved = D.moved || Math.hypot(ev.clientX - D.x0, ev.clientY - D.y0) > 4;
    if (!moved) return;
    D.moved = true;
    const slot = pointToSlot(rect, ev.clientX, ev.clientY);
    if (mode === "move") {
      const dMin = slot.min - start.min;
      const r = moveRange(b.start, b.end, dMin);
      let idx = startDayIdx;
      if (orient === "v") idx = clamp(startDayIdx + (slot.col - start.col), 0, sf.days.length - 1);
      D.result = { day: sf.days[idx], start: r.start, end: r.end };
      if (orient === "v" && b.el.parentNode !== sf.colEls[idx]) { sf.colEls[idx].appendChild(b.el); }
      place(b.el, orient, r.start, r.end, orient === "h" ? { row: b.row, span: b.span || 1 } : { row: 0, rows: 1 });
      posLabel(b.el, r.start, r.end);
    } else {
      const edge = mode === "resize-start" ? "start" : "end";
      const dMin = slot.min - start.min;
      const r = resizeRange(b.start, b.end, edge, dMin);
      D.result = { day: b.day, start: r.start, end: r.end };
      place(b.el, orient, r.start, r.end, { row: b.row, rows: b.rows, span: b.span || 1 });
      posLabel(b.el, r.start, r.end);
    }
  };
  const finish = (ev, cancelled) => {
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
    document.removeEventListener("pointercancel", onCancel);
    S.drag = null;
    b.el.classList.remove("dragging");
    if (cancelled) { P.paint(); P.dragEnded(); return; }
    if (!D.moved) {
      if (b.ev) P.openForm({ mode: "edit", ev: b.ev });
      P.dragEnded();
      return;
    }
    commit(P, mode, b, D.result);
  };
  const onUp = (ev) => finish(ev, false);
  const onCancel = (ev) => finish(ev, true);
  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
  document.addEventListener("pointercancel", onCancel);
}

function commit(P, mode, b, r) {
  if (!r) { P.paint(); P.dragEnded(); return; }
  const ev = b.ev;
  const to = { day: r.day, start: fromMin(r.start), end: fromMin(r.end) };
  const unchanged = r.day === ev.day && r.start === b.start && r.end === b.end;
  if (unchanged) { P.paint(); P.dragEnded(); return; }
  if (ev.recur && r.day === ev.day) {
    P.chooser({
      title: "Recurring event: " + ev.title,
      text: "New time " + to.start + " to " + to.end + ". Apply it to:",
      options: [
        { label: "This occurrence", run: () => P.act.moveEvent(ev, to) },
        { label: "The series", run: () => P.act.retimeSeries(ev, to) },
      ],
      onCancel: () => P.paint(),
    });
  } else {
    P.act.moveEvent(ev, to);
  }
  P.dragEnded();
}
