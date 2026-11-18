/* Team chart (docs/v2/contracts.md §8.4): per session, helper runs grouped by task, no arrows (changed 2026-11-16).
   Left: the orchestrator row, then one header row per task group (fold button, name, type chip, run count and time
   range) and, inside an open group, one row per helper TYPE (merged runs: "implementer #1, #2 (2 rounds)", the latest
   brief label on the same line). Right: one SVG on a shared time axis: the orchestrator's lifeline, a thin span bar per
   group header, every run of a type as a bar on that type's row (parallel runs stack at half height), verdict chips.
   Zoom and pan: a view window per session (viewOf, zoomView, panView, repaintPlot redraws the SVG only). graphHtml is pure (markup only) so the node tests can run it; updateNow moves the running edges and re-places the
   labels (running text, chips) in place, measured against the real plot width. */
import { T, art } from "./team-creatures.js";
import { esc, fmtT } from "../kit/format.js";

export const LIFE_H = 32;
export const HEAD_H = 30;
export const HEAD_TALL = 46;     /* an open group with a long name: the name wraps to two lines */
const LONG_NAME = 28;
export const ROW_H = 24;
export const AXIS_H = 24;
export const LEFT_W = 420;       /* keep equal to .tg grid-template-columns in team.css */
export const PLOT_PAD = 6 + 12;  /* .tg-plot horizontal padding */
const BAR_H = 12;
const CHIP_H = 16;
const GAP = 8;
const CHAR_W = 6.7;              /* 10.5px mono text plus its letter spacing */
const STEP = 1 / 12;             /* the axis ends on 5 minutes, so a lane is repainted at most every 5 minutes, not every poll */
const MIN_TICK_FRAC = 0.07;      /* no tick label closer to the left edge than about 40px of a 600px plot */
const MAX_TICK_FRAC = 0.96;      /* ... nor to the right edge (the plot clips) */
export const MARGIN_H = 1;       /* a view may reach 1 h before the first and after the last activity */
export const MIN_WIN_H = 10 / 60;/* tightest zoom: 10 minutes */
export const SHORT_H = 3;        /* a session shorter than this shows its whole span */
export const BACK_H = 2, FWD_H = 1;   /* default window: the last 2 h and the next hour */
export const OTHER_KEY = "~other";
export const OTHER_TITLE = "Other helper runs";
const BUILDERS = new Set(["builder", "writer", "analyst"]);   /* implementer, drafter, researcher */

/* short codes on the chart (R, NC, ESC); the full words go to title and aria-label */
const VERDICT = {
  READY: { text: "R", full: "READY", color: "var(--ok)" },
  NEEDS_CHANGES: { text: "NC", full: "NEEDS CHANGES", color: "var(--warn)" },
  ESCALATE: { text: "ESC", full: "ESCALATE", color: "var(--crit)" },
};
export function chipW(text) { return Math.max(26, Math.round(String(text).length * 6.4) + 14); }
export function verdictOf(v) {
  if (!v) return null;
  const k = String(v).toUpperCase().replace(/[\s-]+/g, "_");
  if (VERDICT[k]) return { ...VERDICT[k], w: chipW(VERDICT[k].text) };
  const t = String(v).toUpperCase().replace(/_/g, " ");
  return { text: t, full: t, color: "var(--ink-2)", w: chipW(t) };
}

function tierOf(agent) { return (T[agent] || {}).tier || "sonnet"; }
function nameOf(h) { return (T[h.agent] || {}).name || h.type || h.agent; }

/** The shared axis {t0, t1} in hours, ends on 5 minutes. nowH only matters while something runs. */
export function axisOf(session, nowH) {
  const hs = session.helpers || [];
  const span = session.span || { start_h: nowH, end_h: nowH };
  let end = span.end_h;
  if (session.status === "working" && nowH != null) end = Math.max(end, nowH);
  hs.forEach((h) => { end = Math.max(end, h.stop_h != null ? h.stop_h : (nowH != null ? nowH : h.start_h)); });
  let start = span.start_h;
  hs.forEach((h) => { start = Math.min(start, h.start_h); });
  const t0 = Math.floor(start / STEP + 1e-9) * STEP;
  let t1 = Math.ceil((end + 0.02) / STEP) * STEP;
  if (t1 - t0 < 0.5) t1 = t0 + 0.5;
  return { t0, t1 };
}

function frac(ax, h) { return Math.max(0, Math.min(1, (h - ax.t0) / (ax.t1 - ax.t0))); }
function pctOf(ax, h) { return (frac(ax, h) * 100).toFixed(3) + "%"; }
function inWin(ax, s, e) { return e > ax.t0 && s < ax.t1; }

/** Tick step in hours by window size (5, 10, 15, 30 or 60 minutes): about ten ticks at most. */
export function tickStep(range) {
  const min = range <= 0.75 ? 5 : range <= 1.5 ? 10 : range <= 2.5 ? 15 : range <= 5 ? 30 : 60;
  return min / 60;
}
function ticksOf(ax) {
  const step = tickStep(ax.t1 - ax.t0), out = [];
  for (let t = Math.ceil(ax.t0 / step - 1e-9) * step; t <= ax.t1 + 1e-9; t += step) out.push(t);
  return out;
}

/* ── view window (zoom and pan), hours since midnight ── */
/** The furthest a view may reach: the session axis plus 1 h each side (kept inside the day). */
export function boundsOf(session, nowH) {
  const ax = axisOf(session, nowH);
  return { lo: Math.max(0, ax.t0 - MARGIN_H), hi: Math.min(24, ax.t1 + MARGIN_H) };
}
/** A window forced inside the bounds, at least 10 minutes and at most the whole range wide. */
export function clampView(session, nowH, v) {
  const { lo, hi } = boundsOf(session, nowH);
  const maxW = hi - lo;
  const w = Math.max(Math.min(MIN_WIN_H, maxW), Math.min(maxW, v.t1 - v.t0));
  let t0 = v.t0;
  if (t0 + w > hi) t0 = hi - w;
  if (t0 < lo) t0 = lo;
  return { t0, t1: t0 + w };
}
function lastOf(session, nowH) {
  let end = (session.span || {}).end_h;
  if (end == null) end = nowH || 0;
  (session.helpers || []).forEach((h) => { end = Math.max(end, h.stop_h != null ? h.stop_h : (nowH != null ? nowH : h.start_h)); });
  if (session.status === "working" && nowH != null) end = Math.max(end, nowH);
  return end;
}
/** The default window: the whole axis for a session shorter than 3 h, else 2 h back and 1 h ahead of now (working) or of the
    last activity (stopped), rounded up to 5 minutes so a live lane is repainted at most every 5 minutes. */
export function defaultView(session, nowH) {
  const full = axisOf(session, nowH);
  if (full.t1 - full.t0 < SHORT_H) return full;
  const q = Math.ceil((lastOf(session, nowH) + 0.02) / STEP - 1e-9) * STEP;
  return clampView(session, nowH, { t0: q - BACK_H, t1: q + FWD_H });
}
/** The window shown: the operator's manual view (clamped) over the default. */
export function viewOf(session, nowH, view) {
  return view ? clampView(session, nowH, view) : defaultView(session, nowH);
}
/** Zoom by factor (below 1 zooms in) keeping the time anchorT at the same place on the plot. */
export function zoomView(session, nowH, v, anchorT, factor) {
  const { lo, hi } = boundsOf(session, nowH);
  const w0 = v.t1 - v.t0;
  const w = Math.max(Math.min(MIN_WIN_H, hi - lo), Math.min(hi - lo, w0 * factor));
  const f = Math.max(0, Math.min(1, (anchorT - v.t0) / w0));
  return clampView(session, nowH, { t0: anchorT - f * w, t1: anchorT - f * w + w });
}
/** The view after one wheel event over the plot (left, width: the plot box in px; wheel up, deltaY < 0, zooms in). */
export function wheelView(session, nowH, cur, ev, left, width) {
  const anchor = cur.t0 + Math.max(0, Math.min(1, (ev.clientX - left) / (width || 1))) * (cur.t1 - cur.t0);
  const d = Math.max(-300, Math.min(300, ev.deltaY * (ev.deltaMode === 1 ? 16 : 1)));
  return zoomView(session, nowH, cur, anchor, Math.pow(1.002, d));
}
export function panView(session, nowH, v, dt) {
  return clampView(session, nowH, { t0: v.t0 + dt, t1: v.t1 + dt });
}
/** The scroll bar thumb as fractions of its track; full = nothing to scroll. */
export function thumbOf(session, nowH, v) {
  const { lo, hi } = boundsOf(session, nowH);
  const r = hi - lo || 1;
  return { left: (v.t0 - lo) / r, width: Math.min(1, (v.t1 - v.t0) / r), full: v.t1 - v.t0 >= r - 1e-6 };
}

/** aria values of the time bar thumb: minutes from the start of the scrollable axis to the window start (0 .. the latest
    start that still fits the window), with a readable text. */
export function thumbAria(session, nowH, v) {
  const { lo, hi } = boundsOf(session, nowH);
  const hm = (h) => { const m = Math.round(h * 60) % 1440; return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"); };
  return { min: 0, max: Math.max(0, Math.round((hi - lo - (v.t1 - v.t0)) * 60)), now: Math.max(0, Math.round((v.t0 - lo) * 60)),
           text: hm(v.t0) + " to " + hm(v.t1) };
}
/** Keyboard on the time bar: ArrowLeft/ArrowRight pan by 10 % of the window, Home/End jump to the bounds, + / - zoom
    around the window centre, Escape or 0 reset. Returns undefined for any other key (not handled), else
    {view} (view null = back to the default window). */
export function keyView(session, nowH, v, key) {
  const w = v.t1 - v.t0, c = (v.t0 + v.t1) / 2;
  switch (key) {
    case "ArrowLeft": return { view: panView(session, nowH, v, -0.1 * w) };
    case "ArrowRight": return { view: panView(session, nowH, v, 0.1 * w) };
    case "Home": return { view: panView(session, nowH, v, -1e6) };
    case "End": return { view: panView(session, nowH, v, 1e6) };
    case "+": case "=": return { view: zoomView(session, nowH, v, c, 0.8) };
    case "-": case "_": return { view: zoomView(session, nowH, v, c, 1.25) };
    case "Escape": case "0": return { view: null };
    default: return undefined;
  }
}

/* ── groups ── */
/** Task groups of a session: [{key, id, meta, runs, types: [{key, agent, type, runs}], start_h, end_h (null while running), running, name}].
    Order: session.tasks, then task ids only seen on runs, the "Other helper runs" group last. */
export function groupsOf(session) {
  const hs = session.helpers || [];
  const byTask = new Map();
  hs.forEach((h) => {
    const k = h.task == null ? OTHER_KEY : String(h.task);
    if (!byTask.has(k)) byTask.set(k, []);
    byTask.get(k).push(h);
  });
  const metas = new Map();
  (session.tasks || []).forEach((t) => metas.set(t.id == null ? OTHER_KEY : String(t.id), t));
  const keys = [];
  metas.forEach((_m, k) => { if (byTask.has(k) && k !== OTHER_KEY) keys.push(k); });
  byTask.forEach((_r, k) => { if (k !== OTHER_KEY && !keys.includes(k)) keys.push(k); });
  if (byTask.has(OTHER_KEY)) keys.push(OTHER_KEY);
  return keys.map((key) => {
    const runs = byTask.get(key);
    const meta = metas.get(key) || { id: key === OTHER_KEY ? null : key, type: null };
    const types = [];
    runs.forEach((h) => {
      const tk = h.type || h.agent;
      let r = types.find((x) => x.key === tk);
      if (!r) { r = { key: tk, agent: h.agent, type: h.type, runs: [] }; types.push(r); }
      r.runs.push(h);
    });
    const running = runs.some((h) => h.running);
    const g = {
      key, id: meta.id == null ? null : meta.id, meta, runs, types, running,
      start_h: Math.min(...runs.map((h) => h.start_h)),
      end_h: running ? null : Math.max(...runs.map((h) => h.stop_h)),
    };
    g.name = groupName(g);
    return g;
  });
}

/** Plan item title, else the brief label of the first builder run, else the first run's label, else the type. */
function groupName(g) {
  if (g.id == null) return OTHER_TITLE;
  const m = g.meta || {};
  if (m.plan_title) return m.plan_title;
  const b = g.runs.find((h) => BUILDERS.has(h.agent) && h.label);
  if (b) return b.label;
  const f = g.runs.find((h) => h.label);
  if (f) return f.label;
  return m.type ? String(m.type).charAt(0).toUpperCase() + String(m.type).slice(1) : "Task " + g.id;
}

/** Default fold state, group key -> open: groups with a running helper, else the most recent group. */
export function foldDefaults(session) {
  const gs = groupsOf(session), out = new Map();
  let open = gs.filter((g) => g.running);
  if (!open.length && gs.length) {
    open = [gs.reduce((a, g) => (Math.max(...g.runs.map((h) => h.start_h)) >= Math.max(...a.runs.map((h) => h.start_h)) ? g : a))];
  }
  gs.forEach((g) => out.set(g.key, open.includes(g)));
  return out;
}

/** Open state of every group: the user's toggles (folds: Map key -> bool) over the defaults. */
export function openMap(session, folds) {
  const d = foldDefaults(session);
  d.forEach((_v, k) => { if (folds && folds.has(k)) d.set(k, !!folds.get(k)); });
  return d;
}

/* ── label placement ── */
/** Where a running label of width w goes next to a bar from xsPx to xePx in a plot of width plotW: right of the end if it
    fits, else inside the bar end if the bar is long enough, else left of the bar start. (Verdict chips never flip.) */
export function place(xsPx, xePx, w, plotW) {
  if (xePx + GAP + w <= plotW) return { at: "e", off: GAP, anchor: "start" };
  if (xePx - xsPx >= w + 2 * GAP) return { at: "e", off: -GAP, anchor: "end" };
  return { at: "s", off: -GAP, anchor: "end" };
}

/** Inner markup of a label wrapper from its parameters; e null = open-ended (running, ends at nowH).
    Empty when the bar is wholly outside the view window. */
function labInner(p, ax, plotW, nowH) {
  const e = p.e == null ? Math.max(nowH != null ? nowH : p.s, p.s) : p.e;
  if (!inWin(ax, p.s, Math.max(e, p.s + 1e-6))) return "";
  const xs = frac(ax, p.s) * plotW, xe = frac(ax, e) * plotW;
  if (p.kind === "run") {
    const w = Math.round(p.text.length * CHAR_W);
    const pl = place(xs, xe, w, plotW);
    const at = pctOf(ax, pl.at === "e" ? e : p.s);
    return `<text class="tg-runtxt" x="${at}" dx="${pl.off}" y="${p.cy + 4}" text-anchor="${pl.anchor}">${esc(p.text)}</text>`;
  }
  /* a chip always sits to the right of the bar end; past the plot's right edge the plot clips it (pan to see it) */
  const full = esc(p.full || p.text);
  return `<svg class="tg-chip" x="${pctOf(ax, e)}" y="${p.cy - CHIP_H / 2}" width="1" height="${CHIP_H}" overflow="visible">`
    + `<g transform="translate(${GAP},0)" role="img" aria-label="${full}" title="${full}"><title>${full}</title>`
    + `<rect width="${p.w}" height="${CHIP_H}" rx="${CHIP_H / 2}" style="stroke:${esc(p.color)}"/>`
    + `<text x="${p.w / 2}" y="${CHIP_H - 5}" text-anchor="middle" style="fill:${esc(p.color)}">${esc(p.text)}</text></g></svg>`;
}

function labWrap(p, ax, plotW, nowH) {
  return `<g class="tg-lab" data-kind="${p.kind}" data-s="${p.s}" data-e="${p.e == null ? "" : p.e}" data-cy="${p.cy}" data-w="${p.w || 0}"`
    + ` data-text="${esc(p.text)}" data-full="${esc(p.full || "")}" data-c="${esc(p.color || "")}">${labInner(p, ax, plotW, nowH)}</g>`;
}

/* ── rows ── */
function fmtRange(g) { return g.running ? fmtT(g.start_h) + "-now" : fmtT(g.start_h) + "-" + fmtT(g.end_h); }
function rowsWord(n) { return n === 1 ? "1 run" : n + " runs"; }

function leftRow(cls, h, inner, extra = "") {
  return `<div class="tg-row ${cls}"${extra} style="height:${h}px">${inner}</div>`;
}

/** Bars of one type row on lanes: overlapping runs get their own lane. Returns [{h, lane}] and the lane count. */
function lanesOf(runs, nowH) {
  const ends = [], out = [];
  runs.slice().sort((a, b) => a.start_h - b.start_h).forEach((h) => {
    const e = h.running ? Math.max(nowH != null ? nowH : h.start_h, h.start_h) : h.stop_h;
    let lane = ends.findIndex((x) => x <= h.start_h + 1e-9);
    if (lane < 0) { lane = ends.length; ends.push(0); }
    ends[lane] = e;
    out.push({ h, lane });
  });
  return { items: out, lanes: Math.max(1, ends.length) };
}

function idsText(runs) {
  const ns = runs.map((h) => h.n);
  if (ns.length > 3) return `#${ns[0]}-#${ns[ns.length - 1]}`;
  return ns.map((n) => "#" + n).join(", ");
}

/** Left column and plot SVG for one session over the window ax. */
function build(session, nowH, ax, plotW, folds) {
  const sid = session.sid;
  const working = session.status === "working";
  const span = session.span || { start_h: ax.t0, end_h: ax.t0 };
  const lifeEnd = working && nowH != null ? Math.max(nowH, span.end_h) : span.end_h;
  const groups = groupsOf(session);
  const open = openMap(session, folds);

  let left = leftRow(working ? "work" : "done", LIFE_H, `<span class="a">${art("chief", sid + ":chief")}</span><span class="tx"><span class="nm">Orchestrator</span><span class="lb">main session</span></span>`);
  let lines = "", bars = "", labs = "", gbars = "";
  let y = LIFE_H;
  lines += `<line class="tg-rowline" x1="0" x2="100%" y1="${y}" y2="${y}"/>`;
  groups.forEach((g, gi) => {
    const isOpen = !!open.get(g.key);
    const verdict = g.meta && g.meta.final_verdict;
    const count = g.meta && g.meta.runs != null ? Math.max(g.meta.runs, g.runs.length) : g.runs.length;
    const label = isOpen ? "Fold" : "Unfold";
    const hh = isOpen && g.name.length > LONG_NAME ? HEAD_TALL : HEAD_H;
    const gtype = g.meta && g.meta.type ? String(g.meta.type).toLowerCase() : "";
    const sessTypes = [session.type, String(session.tag || "").replace(/^declared:\s*/i, "")].map((x) => String(x || "").toLowerCase());
    left += leftRow(hh > HEAD_H ? "tg-head tall" : "tg-head", hh,
      `<button type="button" class="tg-gt" data-sid="${esc(sid)}" data-gk="${esc(g.key)}" aria-expanded="${isOpen}" aria-label="${label} ${esc(g.name)}" title="${esc(g.name)}">`
      + `<span class="chev" aria-hidden="true"></span><span class="gn${g.running ? " live" : ""}">${esc(g.name)}</span></button>`
      + (gtype && !sessTypes.includes(gtype) ? `<span class="gty">${esc(g.meta.type)}</span>` : "")
      + `<span class="gct">${esc(rowsWord(count))} · ${esc(fmtRange(g))}</span>`, ` data-g="${gi}"`);
    const cy = y + hh / 2;
    const gEnd = g.running ? Math.max(nowH != null ? nowH : g.start_h, g.start_h) : g.end_h;
    if (inWin(ax, g.start_h, gEnd)) {
      const xs = pctOf(ax, g.start_h), xe = pctOf(ax, gEnd);
      gbars += `<rect class="tg-gspan${g.running ? " tg-gspan-run" : ""}" data-s="${g.start_h}" x="${xs}" y="${cy - 3}" width="${Math.max(parseFloat(xe) - parseFloat(xs), 0.5).toFixed(3)}%" height="6" rx="3"/>`;
    }
    const v = verdictOf(verdict);
    if (v) labs += labWrap({ kind: "chip", s: g.start_h, e: g.running ? null : g.end_h, cy, w: v.w, text: v.text, full: v.full, color: v.color }, ax, plotW, nowH);
    y += hh;
    lines += `<line class="tg-rowline" x1="0" x2="100%" y1="${y}" y2="${y}"/>`;
    if (!isOpen) return;
    g.types.forEach((r, ri) => {
      const key = gi + "-" + ri, latest = r.runs[r.runs.length - 1];
      const running = r.runs.some((h) => h.running);
      const resumed = r.runs.some((h) => h.resumed);
      const n = r.runs.length;
      const nm = `${nameOf(r)} ${idsText(r.runs)}${n > 1 ? ` (${n} rounds)` : ""}${resumed ? " resumed" : ""}`;
      const brief = latest.label || "(no brief label)";
      /* two fixed columns: the name cell has one width on every row, so the brief starts at the same x everywhere */
      left += leftRow(`tg-run ${running ? "work" : "done"}`, ROW_H,
        `<span class="a">${art(r.agent, sid + ":" + g.key + ":" + r.key)}</span><span class="nm" title="${esc(nm)}">${esc(nameOf(r))} ${esc(idsText(r.runs))}`
        + `${n > 1 ? ` <i class="rs">(${n} rounds)</i>` : ""}${resumed ? ` <i class="rs">resumed</i>` : ""}</span>`
        + `<span class="lb" title="${esc(brief)}">${esc(brief)}</span>`, ` data-r="${key}"`);
      const { items, lanes } = lanesOf(r.runs, nowH);
      const laneH = (ROW_H - 4) / lanes, barH = lanes > 1 ? Math.max(3, laneH - 1) : BAR_H;
      items.forEach(({ h, lane }) => {
        const top = y + 2 + lane * laneH + (lanes > 1 ? 0 : (ROW_H - 4 - BAR_H) / 2);
        const endH = h.running ? Math.max(nowH != null ? nowH : h.start_h, h.start_h) : h.stop_h;
        if (inWin(ax, h.start_h, endH)) {       /* a bar partly outside shows its visible part (positions clamp to the plot) */
          const xs2 = pctOf(ax, h.start_h);
          const w = Math.max(parseFloat(pctOf(ax, endH)) - parseFloat(xs2), 0.5).toFixed(3) + "%";
          bars += `<rect class="tg-bar${h.running ? " tg-runbar" : ""}" data-r="${key}" data-a="${esc(h.agent_id)}" data-s="${h.start_h}" x="${xs2}" y="${top.toFixed(2)}" width="${w}" height="${barH.toFixed(2)}" rx="3" style="fill:var(--m-${tierOf(h.agent)})"/>`;
        }
        const rcy = top + barH / 2;
        if (h.running) {
          labs += labWrap({ kind: "run", s: h.start_h, e: null, cy: rcy, text: "running" }, ax, plotW, nowH);
        } else if (r.runs.length === 1) {
          const hv = verdictOf(h.verdict);
          if (hv) labs += labWrap({ kind: "chip", s: h.start_h, e: h.stop_h, cy: rcy, w: hv.w, text: hv.text, full: hv.full, color: hv.color }, ax, plotW, nowH);
        }
      });
      if (r.runs.length > 1) {          /* a merged row: ONE chip after the last bar, the verdicts in order ("NC → NC → R") */
        const done = items.map((x) => x.h).filter((h) => !h.running).sort((a, b) => a.start_h - b.start_h);
        const vs = done.filter((h) => h.verdict).map((h) => verdictOf(h.verdict));
        const last = done[done.length - 1];
        if (vs.length && last) {
          const text = vs.map((v) => v.text).join(" → ");
          labs += labWrap({ kind: "chip", s: last.start_h, e: last.stop_h, cy: y + ROW_H / 2, w: chipW(text), text,
            full: vs.map((v) => v.full).join(" → "), color: vs[vs.length - 1].color }, ax, plotW, nowH);
        }
      }
      y += ROW_H;
      lines += `<line class="tg-rowline" x1="0" x2="100%" y1="${y}" y2="${y}"/>`;
    });
  });
  left += `<div class="tg-axis-pad" style="height:${AXIS_H}px"></div>`;
  const H = y + AXIS_H;

  let g = "";
  ticksOf(ax).forEach((t) => {
    const x = pctOf(ax, t);
    const f = (t - ax.t0) / (ax.t1 - ax.t0);
    const lab = f >= MIN_TICK_FRAC && f <= MAX_TICK_FRAC;
    g += `<line class="tg-grid" x1="${x}" x2="${x}" y1="0" y2="${H - AXIS_H}"/>${lab ? `<text class="tg-tick" x="${x}" y="${H - 7}" text-anchor="middle">${fmtT(t)}</text>` : ""}`;
  });
  const yL = LIFE_H / 2;
  if (inWin(ax, span.start_h, Math.max(lifeEnd, span.start_h + 1e-6))) {
    g += lines + `<line class="tg-life" x1="${pctOf(ax, span.start_h)}" x2="${pctOf(ax, lifeEnd)}" y1="${yL}" y2="${yL}"/>`
      + (span.start_h >= ax.t0 ? `<circle class="tg-lifedot" cx="${pctOf(ax, span.start_h)}" cy="${yL}" r="3"/>` : "");
  } else g += lines;
  const svg = `<svg class="tg-svg" width="100%" height="${H}" role="img" aria-label="Timeline of helper runs for this session, grouped by task">${g}${gbars}${bars}${labs}</svg>`;
  return { left, svg };
}

/** The time bar is hidden when the whole range shows and the view is not manual, except while its thumb has focus
    (Escape or 0 at full range must not hide the focused element). */
export function barHidden(full, manual, focused) { return !!full && !manual && !focused; }

/** The scroll bar under the plot: a track with a draggable thumb, and "Reset view" once the view was changed by hand. */
function zbarHtml(session, nowH, ax, manual) {
  const th = thumbOf(session, nowH, ax);
  const pc = (x) => (x * 100).toFixed(3) + "%";
  const a = thumbAria(session, nowH, ax);
  return `<div class="tg-zbar" data-sid="${esc(session.sid)}"${th.full && !manual ? " hidden" : ""}>`
    + `<div class="tg-ztrack" title="Scroll over the chart to zoom, drag to move"><div class="tg-zthumb" style="left:${pc(th.left)};width:${pc(th.width)}" role="scrollbar" tabindex="0" aria-label="Time window" aria-orientation="horizontal" aria-valuemin="${a.min}" aria-valuemax="${a.max}" aria-valuenow="${a.now}" aria-valuetext="${a.text}"></div></div>`
    + `<button type="button" class="tg-reset"${manual ? "" : " hidden"}>Reset view</button></div>`;
}

/** The chart for one session. session: {sid, status, helpers, helpers_total, span, tasks}; nowH: hours since midnight.
    opts: {plotW (px of the plot, default 520), folds (Map group key -> open, the user's toggles),
    view ({t0, t1} the operator's zoom/pan window, kept per session by the caller; default window when absent)}. */
export function graphHtml(session, nowH, opts = {}) {
  const hs = session.helpers || [];
  const sid = session.sid;
  const plotW = Math.max(200, Number(opts.plotW) || 520);
  const ax = viewOf(session, nowH, opts.view);
  const { left, svg } = build(session, nowH, ax, plotW, opts.folds);
  const note = session.helpers_total > hs.length
    ? `<p class="tg-note">Showing the latest ${hs.length} of ${esc(session.helpers_total)} runs.</p>` : "";
  const empty = hs.length ? "" : `<p class="tg-note">No helper was launched in this session today.</p>`;
  return `<div class="tg-wrap"><div class="tscroll" data-keep-scroll><div class="tg" data-sid="${esc(sid)}" data-t0="${ax.t0}" data-t1="${ax.t1}" data-pw="${plotW}">`
    + `<div class="tg-left">${left}</div><div class="tg-plot"><div class="tg-svgbox">${svg}</div>${zbarHtml(session, nowH, ax, !!opts.view)}</div></div></div>${note}${empty}</div>`;
}

/** Repaints only the SVG, the scroll bar and the reset button of an already painted chart for a new view (zoom, pan,
    reset); the left column is untouched, so the creatures keep running. view null = back to the default window. */
export function repaintPlot(root, session, nowH, opts = {}) {
  const g = root.querySelector(".tg");
  const box = g && g.querySelector(".tg-svgbox");
  if (!box) return null;
  const old = box.querySelector(".tg-svg");
  const plotW = Math.max(200, (old && old.clientWidth) || parseFloat(g.dataset.pw) || 520);
  const ax = viewOf(session, nowH, opts.view);
  box.innerHTML = build(session, nowH, ax, plotW, opts.folds).svg;
  g.dataset.t0 = ax.t0; g.dataset.t1 = ax.t1;
  const bar = g.querySelector(".tg-zbar");
  if (bar) {
    const th = thumbOf(session, nowH, ax), manual = !!opts.view;
    const thumb = bar.querySelector(".tg-zthumb"), reset = bar.querySelector(".tg-reset");
    if (thumb) {
      const a = thumbAria(session, nowH, ax);
      thumb.setAttribute("aria-valuemin", a.min); thumb.setAttribute("aria-valuemax", a.max);
      thumb.setAttribute("aria-valuenow", a.now); thumb.setAttribute("aria-valuetext", a.text);
    }
    if (thumb) { thumb.style.left = (th.left * 100).toFixed(3) + "%"; thumb.style.width = (th.width * 100).toFixed(3) + "%"; }
    bar.hidden = barHidden(th.full, manual, !!thumb && typeof document !== "undefined" && document.activeElement === thumb);
    if (reset) reset.hidden = !manual;
  }
  return ax;
}

/** Moves the "now" edge of running bars, group spans and a working lifeline inside an already painted chart, and
    re-places the labels against the plot's real width (running text flips instead of leaving the plot). */
export function updateNow(root, session, nowH) {
  const g = root.querySelector(".tg");
  if (!g || nowH == null) return;
  const ax = { t0: parseFloat(g.dataset.t0), t1: parseFloat(g.dataset.t1) };
  const svg = g.querySelector(".tg-svg");
  const plotW = (svg && svg.clientWidth) || parseFloat(g.dataset.pw) || 520;
  const grow = (r) => {
    const s = parseFloat(r.dataset.s);
    const xs = pctOf(ax, s), xe = pctOf(ax, Math.max(nowH, s));
    r.setAttribute("width", Math.max(parseFloat(xe) - parseFloat(xs), 0.5).toFixed(3) + "%");
  };
  g.querySelectorAll(".tg-runbar, .tg-gspan-run").forEach(grow);
  g.querySelectorAll(".tg-lab").forEach((el) => {
    const d = el.dataset;
    el.innerHTML = labInner({ kind: d.kind, s: parseFloat(d.s), e: d.e === "" ? null : parseFloat(d.e), cy: parseFloat(d.cy),
      w: parseFloat(d.w), text: d.text, full: d.full, color: d.c }, ax, plotW, nowH);
  });
  if (session.status === "working") {
    const l = g.querySelector(".tg-life");
    if (l) l.setAttribute("x2", pctOf(ax, Math.max(nowH, (session.span || {}).end_h || 0)));
  }
}

/** Row highlight: row key (left label and its bars) at full contrast, the other bars slightly dimmed. null clears it. */
export function setHot(root, key) {
  const g = root.querySelector(".tg");
  if (!g) return;
  g.classList.toggle("hovering", key != null);
  g.querySelectorAll(".hot").forEach((el) => el.classList.remove("hot"));
  if (key == null) return;
  g.querySelectorAll(`[data-r="${String(key).replace(/[^0-9-]/g, "")}"]`).forEach((el) => el.classList.add("hot"));
}
