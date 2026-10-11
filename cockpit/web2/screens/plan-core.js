/* Plan screen: pure helpers (no DOM), tested by cockpit/test_web2_plan.mjs.
   Time is handled in whole minutes since midnight; days are ISO strings "YYYY-MM-DD". */
import { assignLanes } from "../kit/format.js";

export const SNAP = 15;            // minutes
export const AXIS_MIN = 420;       // 07:00, left edge of the axis (sleep hours are not planned)
export const AXIS_MAX = 1320;      // 22:00, right edge of the axis
export const EDIT_MIN = 420;       // the engine refuses events outside 07:00-22:00
export const EDIT_MAX = 1320;
export const DEFAULT_LEN = 60;

const p2 = (n) => (n < 10 ? "0" : "") + n;

/* ---------- time ---------- */
export function toMin(hhmm) {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(hhmm == null ? "" : hhmm));
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}
export function fromMin(min) {
  const v = Math.max(0, Math.round(min));
  return p2(Math.floor(v / 60)) + ":" + p2(v % 60);
}
export function snap(min, step = SNAP) { return Math.round(min / step) * step; }
export function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

/** Shift a range by `delta` minutes (snapped), keeping its length, inside [lo, hi]. */
export function moveRange(start, end, delta, lo = EDIT_MIN, hi = EDIT_MAX) {
  const len = end - start;
  const s = clamp(snap(start + delta), lo, Math.max(lo, hi - len));
  return { start: s, end: s + len };
}
/** Drag one edge of a range; the other edge stays; the result is at least `minLen` long. */
export function resizeRange(start, end, edge, delta, minLen = SNAP, lo = EDIT_MIN, hi = EDIT_MAX) {
  if (edge === "start") return { start: clamp(snap(start + delta), lo, end - minLen), end };
  return { start, end: clamp(snap(end + delta), start + minLen, hi) };
}
/** A range from a press at `a` to a release at `b`. A click (no drag) gives the default length. */
export function createRange(a, b, lo = EDIT_MIN, hi = EDIT_MAX, len = DEFAULT_LEN) {
  let s = Math.min(a, b), e = Math.max(a, b);
  if (Math.abs(b - a) < SNAP) e = s + len;
  s = clamp(snap(s), lo, hi - SNAP);
  e = clamp(snap(e), s + SNAP, hi);
  if (e - s < SNAP) s = e - SNAP;
  return { start: s, end: e };
}

/* ---------- geometry ---------- */
/** Fraction (0..1) of the axis for a minute, clamped. */
export function minToFrac(min, lo = AXIS_MIN, hi = AXIS_MAX) { return clamp((min - lo) / (hi - lo), 0, 1); }
export function fracToMin(f, lo = AXIS_MIN, hi = AXIS_MAX) { return lo + clamp(f, 0, 1) * (hi - lo); }

/** Which day column and which minute a pointer is over.
    surf = {orient: 'h'|'v', left, top, width, height, cols (1 in day view, 7 in week view), lo, hi}
    'h': time runs along x, one column; 'v': time runs along y, `cols` columns along x. */
export function pointToSlot(surf, x, y) {
  const lo = surf.lo == null ? AXIS_MIN : surf.lo, hi = surf.hi == null ? AXIS_MAX : surf.hi;
  const cols = surf.cols || 1;
  if (surf.orient === "h") {
    return { col: 0, min: fracToMin((x - surf.left) / surf.width, lo, hi) };
  }
  const col = clamp(Math.floor(((x - surf.left) / surf.width) * cols), 0, cols - 1);
  return { col, min: fracToMin((y - surf.top) / surf.height, lo, hi) };
}
export function inside(surf, x, y) {
  return x >= surf.left && x <= surf.left + surf.width && y >= surf.top && y <= surf.top + surf.height;
}

/** Row packing for overlapping blocks. items [{start, end}] in minutes -> {row: [..], rows: n}. */
export function packRows(items) {
  if (!items.length) return { row: [], rows: 0 };
  const row = assignLanes(items.map((i) => ({ start: i.start, end: Math.max(i.end, i.start + 1) })));
  return { row, rows: Math.max(...row) + 1 };
}

/* ---------- dates ---------- */
export function parseDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])) : null;
}
export function isoOf(d) { return d.getFullYear() + "-" + p2(d.getMonth() + 1) + "-" + p2(d.getDate()); }
export function addDays(iso, n) { const d = parseDay(iso); d.setDate(d.getDate() + n); return isoOf(d); }
export function mondayOf(iso) { const d = parseDay(iso); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return isoOf(d); }
export function weekDays(iso) { const m = mondayOf(iso); return [0, 1, 2, 3, 4, 5, 6].map((i) => addDays(m, i)); }
/** Whole days between two ISO days (b - a). */
export function dayDiff(a, b) { return Math.round((Date.UTC(...ymd(b)) - Date.UTC(...ymd(a))) / 86400000); }
function ymd(iso) { const d = parseDay(iso); return [d.getFullYear(), d.getMonth(), d.getDate()]; }
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function dowOf(iso) { return DOW[parseDay(iso).getDay()]; }
export function dayShort(iso) { const d = parseDay(iso); return DOW[d.getDay()] + " " + d.getDate() + " " + MON[d.getMonth()]; }
export function rangeLabel(view, iso) {
  if (view === "day") return dayShort(iso) + " " + parseDay(iso).getFullYear();
  const days = weekDays(iso), a = parseDay(days[0]), z = parseDay(days[6]);
  const sameMonth = a.getMonth() === z.getMonth();
  return a.getDate() + (sameMonth ? "" : " " + MON[a.getMonth()]) + " to " + z.getDate() + " " + MON[z.getMonth()] + " " + z.getFullYear();
}
/** The fetch range for a view: always the whole ISO week, so day navigation inside a week needs no fetch. */
export function fetchRange(iso) { const d = weekDays(iso); return { from: d[0], to: d[6] }; }
export function dayNum(iso) { return Math.round(Date.UTC(...ymd(iso)) / 86400000); }
export function dayFromNum(n) { const d = new Date(n * 86400000); return d.getUTCFullYear() + "-" + p2(d.getUTCMonth() + 1) + "-" + p2(d.getUTCDate()); }

/* ---------- version rules ---------- */
/** Ignore any answer whose version is lower than one already seen; keep the highest. */
export function makeVersionGate() {
  let seen = 0;
  return {
    seen: () => seen,
    /** true when the answer may be applied; the highest version is kept either way. */
    accept(v) {
      if (typeof v !== "number" || !isFinite(v)) return true;
      if (v < seen) return false;
      seen = v;
      return true;
    },
    /** note a version without judging it (errors such as 409 carry the server's current version). */
    note(v) { if (typeof v === "number" && isFinite(v) && v > seen) seen = v; },
  };
}

/* ---------- optimistic edits on the payload's `events` list (pure: they return a new list) ---------- */
export const sameOcc = (e, id, occ) => e.id === id && (occ == null || e.occurrence === occ || e.day === occ);

export function eventsOn(events, day) { return events.filter((e) => e.day === day); }

export function addEvent(events, ev) {
  return events.concat([Object.assign({ occurrence: ev.day, area_source: ev.area ? "explicit" : "none", type: "personal", recur: null, notes: null }, ev, { occurrence: ev.day })]);
}

/** Move/retime one occurrence (or a plain event). A recurring occurrence moved to another day becomes a new one-off. */
export function moveEvent(events, id, occ, to, newId) {
  const src = events.find((e) => sameOcc(e, id, occ));
  if (!src) return events;
  const crossDay = to.day !== src.day;
  if (src.recur && crossDay) {
    const gone = events.filter((e) => e !== src);
    const copy = Object.assign({}, src, { id: newId, day: to.day, occurrence: to.day, recur: null, from_series: id });
    if (to.start != null) { copy.start = to.start; copy.end = to.end; }
    return gone.concat([copy]);
  }
  return events.map((e) => {
    if (e !== src) return e;
    const out = Object.assign({}, e, { day: to.day });
    if (!e.recur) out.occurrence = to.day;
    if (to.start != null) { out.start = to.start; out.end = to.end; }
    return out;
  });
}

/** Patch fields on every occurrence of a series (or the one event). */
export function patchSeries(events, id, patch) {
  return events.map((e) => {
    if (e.id !== id) return e;
    const out = Object.assign({}, e);
    Object.keys(patch).forEach((k) => {
      if (patch[k] === null || patch[k] === undefined) delete out[k];
      else out[k] = patch[k];
    });
    if (out.start == null) { delete out.start; delete out.end; }
    if (out.area_source !== undefined && "area" in patch) out.area_source = patch.area ? "explicit" : "none";
    return out;
  });
}
export function removeEvent(events, id, occ) { return events.filter((e) => !sameOcc(e, id, occ)); }
export function removeSeries(events, id) { return events.filter((e) => e.id !== id); }
export function renameId(events, from, to) { return events.map((e) => (e.id === from ? Object.assign({}, e, { id: to }) : e)); }
/** Put back the entries a failed edit had replaced. */
export function restoreEvents(events, id, saved) { return events.filter((e) => e.id !== id).concat(saved); }
export function entriesOf(events, id) { return events.filter((e) => e.id === id); }

/* ---------- tasks ---------- */
export const STATES = ["todo", "next", "doing", "review", "done"];
export const STATE_LABELS = { todo: "To do", next: "Next", doing: "Doing", review: "Pending review", done: "Done" };

export function groupTasks(list) {
  const out = {};
  STATES.forEach((s) => { out[s] = []; });
  (list || []).forEach((t) => { (out[t.state] || out.todo).push(t); });
  return out;
}
export function setTaskState(tasks, id, state) {
  const out = {};
  Object.keys(tasks).forEach((a) => { out[a] = tasks[a].map((t) => (t.id === id ? Object.assign({}, t, { state }) : t)); });
  return out;
}
export function addTask(tasks, task) {
  const out = Object.assign({}, tasks);
  out[task.area] = (tasks[task.area] || []).concat([task]);
  return out;
}
export function removeTask(tasks, id) {
  const out = {};
  Object.keys(tasks).forEach((a) => { out[a] = tasks[a].filter((t) => t.id !== id); });
  return out;
}
export function renameTask(tasks, from, to) {
  const out = {};
  Object.keys(tasks).forEach((a) => { out[a] = tasks[a].map((t) => (t.id === from ? Object.assign({}, t, { id: to }) : t)); });
  return out;
}
export function isReadOnlyTask(t) { return t.source === "plan.yaml" || String(t.id).includes("/"); }
export function fmtEstimate(min) {
  if (min == null) return "";
  if (min < 60) return min + " min";
  const h = Math.floor(min / 60), m = min % 60;
  return m ? h + " h " + m : h + " h";
}

/* ---------- review tray ---------- */
/** Length (minutes) a review block takes, rounded up to the snap grid. */
export function reviewLen(item) { return Math.max(SNAP, Math.ceil((item && item.review_min ? item.review_min : 20) / SNAP) * SNAP); }
export function markScheduled(tray, id, sched) { return tray.map((t) => (t.id === id ? Object.assign({}, t, { scheduled: sched }) : t)); }
export function removeTray(tray, id) { return tray.filter((t) => t.id !== id); }

/** Everything that sits in the calendar on a day, as blocks. Events first, then read-only placements. */
export function mineBlocks(data, day) {
  const out = [];
  (data.events || []).forEach((e) => {
    if (e.day !== day || !e.start || !e.end) return;
    out.push({ kind: e.review_of ? "review" : "mine", ev: e, start: toMin(e.start), end: toMin(e.end), area: e.area, title: e.title });
  });
  (data.placements || []).forEach((p) => {
    if (p.day !== day || !p.start || !p.end) return;
    out.push({ kind: "ro", pl: p, start: toMin(p.start), end: toMin(p.end), area: p.area, title: (p.task || "Task") + (p.project ? " (" + p.project + ")" : "") });
  });
  return out.filter((b) => b.start != null && b.end != null && b.end > b.start);
}
export function allDayEvents(data, day) { return (data.events || []).filter((e) => e.day === day && !e.start); }

/** The recur object to send after the Repeat control was changed: start from the stored one, apply only what changed.
    repeat = none | daily | weekly; untilIso = null for "no end". Keys the form does not know (count, ...) are kept. */
export function buildRecur(prev, repeat, untilIso, interval) {
  if (repeat === "none") return null;
  const r = Object.assign({}, prev, { freq: repeat });
  if (!r.interval) r.interval = interval || 1;
  if (untilIso) { r.until = untilIso; delete r.count; } else delete r.until;
  return r;
}

/** Recurrence label for the form and tooltips. */
export function recurText(r) {
  if (!r || !r.freq) return "";
  const n = Number(r.interval) > 1 ? "every " + r.interval + " " + (r.freq === "daily" ? "days" : r.freq === "weekly" ? "weeks" : "months") : r.freq;
  return n + (r.until ? " until " + r.until : "");
}

/* ---------- tasks: planning, grouping ---------- */
/** Length (minutes) of the slot a task takes when dragged: its estimate (60 when unknown), on the snap grid, inside the day. */
export function taskLen(t) {
  const m = t && t.estimate_min > 0 ? t.estimate_min : DEFAULT_LEN;
  return clamp(Math.ceil(m / SNAP) * SNAP, SNAP, EDIT_MAX - EDIT_MIN);
}
/** "planned Mon 10:00" for a card, "" when not planned. */
export function plannedLabel(p) {
  if (!p || !p.day || !p.start || !parseDay(p.day)) return "";
  return "planned " + dowOf(p.day) + " " + p.start;
}
/** Why a task can't be dropped on the calendar; "" when it can. (The agents' lane is parked: contracts.md 5.3.) */
export function placeReason(task) {
  if (!task || task.state === "done") return "A finished task can't be planned.";
  return "";
}
export function setPlanned(tasks, id, planned) {
  const out = {};
  Object.keys(tasks).forEach((a) => {
    out[a] = tasks[a].map((t) => {
      if (t.id !== id) return t;
      const n = Object.assign({}, t);
      if (planned) n.planned = planned; else delete n.planned;
      return n;
    });
  });
  return out;
}
export function findTask(tasks, id) {
  for (const a of Object.keys(tasks || {})) { const t = tasks[a].find((x) => x.id === id); if (t) return Object.assign({ area: a }, t); }
  return null;
}
/** The title of an all-day event on `day` (not counting the event `exceptId`), or "". */
export function allDayTitleOn(events, day, exceptId) {
  const e = (events || []).find((x) => x.day === day && !x.start && x.id !== exceptId);
  return e ? String(e.title || "") : "";
}
/** "By state": five columns, every visible area's tasks in area order. areas = [{id}], hidden = {areaId: true}. */
export function tasksByState(tasks, areas, hidden) {
  const out = {};
  STATES.forEach((s) => { out[s] = []; });
  areas.forEach((a) => {
    if (hidden && hidden[a.id]) return;
    (tasks[a.id] || []).forEach((t) => { (out[t.state] || out.todo).push(Object.assign({ area: a.id }, t)); });
  });
  return out;
}
