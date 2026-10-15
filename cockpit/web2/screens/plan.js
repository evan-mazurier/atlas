/* Plan screen (docs/v2/contracts.md §5.3, §8): two timelines, the To schedule memo rail, instant edits.
   The screen changes first and saves in the background (one save at a time, each carrying `base`);
   answers older than one already seen are ignored. Control values live in module state `S`, so the
   15 s poll never clobbers an open form or a drag in progress. Rules for the pure parts: plan-core.js. */
import * as C from "./plan-core.js";
import { renderTime, stopFit } from "./plan-time.js";
import { openEventForm, closeForm, openChooser } from "./plan-form.js";
import { buildMemoRail, renderMemos } from "./plan-side.js";
import { sampleOverlay } from "./plan-sample.js";

let S = null;

function el(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

function todayISO() { return C.isoOf(new Date()); }

function freshState(ctx) {
  const h = location.hash.split("?")[1] || "";
  return {
    ctx,
    dead: false,
    view: "day",
    day: todayISO(),
    data: null,
    rangeKey: "",
    loading: true,
    loadError: "",
    gate: C.makeVersionGate(),
    pending: 0,           // saves queued or running
    epoch: 0,             // bumped on a 409: saves queued before it are dropped
    dropped: 0,
    queue: Promise.resolve(),
    stale: false,         // a poll answer arrived while an edit or drag was busy
    loadSeq: 0,
    drag: null,           // set by plan-time.js while a drag is in progress
    form: null,           // set by plan-form.js while the event form is open
    notice: null,         // {text, kind}
    noticeTimer: 0,
    tmp: 0,
    idMap: {},            // temporary id -> real id, after a create was saved
    taskForm: { title: "", area: "", estimate: 60 },   // the memo quick-add block
    sample: ctx.query.get("sample") === "1" || new URLSearchParams(h).get("sample") === "1",
    els: {},
  };
}

/* ---------- loading ---------- */
async function refresh(force) {
  if (!S || S.dead) return;
  if (!force && (S.pending || S.drag)) { S.stale = true; return; }
  const range = C.fetchRange(S.day);
  const key = range.from + "/" + range.to;
  const seq = ++S.loadSeq;
  let d;
  try {
    d = await S.ctx.api.get("/api/v2/plan?from=" + range.from + "&to=" + range.to);
  } catch (e) {
    if (!S || S.dead || seq !== S.loadSeq) return;
    S.loading = false;
    if (!S.data) S.loadError = "Plan data unavailable (" + (e && e.message || e) + ").";
    else setNotice("Plan not refreshed: " + (e && e.message || e), "warn");
    paint();
    return;
  }
  if (!S || S.dead || seq !== S.loadSeq) return;
  if (S.pending || S.drag) { S.stale = true; return; }
  if (!S.gate.accept(d.version)) { S.loading = false; paint(); return; }
  if (S.sample) d = sampleOverlay(d, todayISO());
  S.data = d;
  S.rangeKey = key;
  S.loading = false;
  S.loadError = "";
  S.stale = false;
  paint();
}

/* ---------- notice line (visible text; never only a tooltip) ---------- */
function setNotice(text, kind) {
  S.notice = text ? { text, kind: kind || "warn" } : null;
  clearTimeout(S.noticeTimer);
  if (text) S.noticeTimer = setTimeout(() => { if (S && !S.dead) { S.notice = null; paintNotice(); } }, 25000);
  paintNotice();
}
function paintNotice() {
  const box = S.els.notice;
  box.textContent = "";
  box.hidden = !S.notice;
  if (!S.notice) return;
  box.className = "plan-notice " + S.notice.kind;
  el("span", null, S.notice.text, box);
  const b = el("button", "plan-link", "Dismiss", box);
  b.type = "button";
  b.addEventListener("click", () => setNotice(null));
}

function describe(o) {
  if (!o || typeof o !== "object") return "";
  const bits = [o.title, o.day, o.start && o.end ? o.start + " to " + o.end : null, o.state].filter(Boolean);
  return bits.join(", ");
}

/* ---------- saving: optimistic apply, serialized sends, reconcile ---------- */
const real = (id) => S.idMap[id] || id;

/** op = {apply(data) -> data, undo(data) -> data, call() -> {path, body}, after?(resp, fresh), what} */
function enqueue(op) {
  S.data = op.apply(S.data);
  op.epoch = S.epoch;
  paint();
  S.pending++;
  S.queue = S.queue.then(() => run(op));
}

async function run(op) {
  const s = S;
  try {
    if (op.epoch !== s.epoch) {            // an earlier save hit a conflict: this one was built on stale data
      s.dropped++;
      S.data = op.undo(S.data);
      return;
    }
    const call = op.call();
    const body = Object.assign({}, call.body, { base: s.gate.seen() });
    const r = await s.ctx.api.post(call.path, body);
    if (S !== s) return;
    const fresh = s.gate.accept(r.version);
    if (op.after) op.after(r, fresh);
  } catch (e) {
    if (S !== s) return;
    S.data = op.undo(S.data);
    const d = e && e.data || {};
    if (e && e.status === 409) {
      S.epoch++;                           // queued saves behind this one are dropped, then the reload is forced
      S.dropped = 0;
      setNotice("Changed elsewhere: reloaded." + (d.current ? " Now: " + describe(d.current) + "." : ""), "warn");
    } else if (e && e.status === 400) {
      setNotice("Not saved: " + e.message, "err");
    } else if (e && e.status === 404) {
      setNotice("Not saved, it no longer exists: " + e.message + ". Reloaded.", "warn");
    } else {
      setNotice("Not saved: " + (e && e.message || e), "err");
    }
    paint();
  } finally {
    if (S === s) {
      S.pending--;
      if (!S.pending) {
        if (S.dropped) setNotice((S.notice ? S.notice.text + " " : "") + S.dropped + " queued " + (S.dropped === 1 ? "edit was" : "edits were") + " dropped.", "warn");
        S.dropped = 0;
        await refresh(true);
      }
    }
  }
}

/* ---------- actions used by the views ---------- */
function newTmp(prefix) { S.tmp += 1; return prefix + "_tmp" + S.tmp; }

const act = {
  /** Edit the stored event (whole event or whole series). */
  editEvent(ev, f) {
    const saved = C.entriesOf(S.data.events, ev.id);
    const patch = { title: f.title, area: f.area || null, notes: f.notes || null };
    if (f.start === undefined) { delete patch.start; }
    else if (f.start != null) { patch.start = C.fromMin(f.start); patch.end = C.fromMin(f.end); } else { patch.start = null; patch.end = null; }
    if (!ev.recur) patch.day = f.day;
    enqueue({
      what: "edit",
      apply: (d) => {
        let events = C.patchSeries(d.events, ev.id, patch);
        if (!ev.recur) events = events.map((e) => (e.id === ev.id ? Object.assign({}, e, { occurrence: f.day }) : e));
        return Object.assign({}, d, { events });
      },
      undo: (d) => Object.assign({}, d, { events: C.restoreEvents(d.events, ev.id, saved) }),
      call: () => ({ path: "/api/v2/plan/event", body: { event: Object.assign(wireEvent(f), { id: real(ev.id) }, ev.recur ? { day: undefined } : {}) } }),
    });
  },

  /** Move or retime one occurrence (or a plain event). `to` = {day, start, end} with "HH:MM" strings (start/end may be null for all-day). */
  moveEvent(ev, to) {
    const saved = C.entriesOf(S.data.events, ev.id);
    const tmp = ev.recur && to.day !== ev.day ? newTmp("evt") : null;
    enqueue({
      what: "move",
      apply: (d) => Object.assign({}, d, { events: C.moveEvent(d.events, ev.id, ev.occurrence, to, tmp) }),
      undo: (d) => Object.assign({}, d, { events: C.restoreEvents(d.events.filter((e) => e.id !== tmp), ev.id, saved) }),
      call: () => ({ path: "/api/v2/plan/event/move", body: Object.assign({ id: real(ev.id), day: to.day }, ev.recur ? { occurrence: ev.occurrence } : {}, to.start ? { start: to.start, end: to.end } : {}) }),
      after: (r) => {
        if (tmp && r.changed && r.changed.id) {
          S.idMap[tmp] = r.changed.id;
          S.data = Object.assign({}, S.data, { events: C.renameId(S.data.events, tmp, r.changed.id) });
        }
      },
    });
  },

  /** Retime a whole series (time of day only; the series keeps its start day). */
  retimeSeries(ev, to) {
    const saved = C.entriesOf(S.data.events, ev.id);
    enqueue({
      what: "retime",
      apply: (d) => Object.assign({}, d, { events: C.patchSeries(d.events, ev.id, { start: to.start, end: to.end }) }),
      undo: (d) => Object.assign({}, d, { events: C.restoreEvents(d.events, ev.id, saved) }),
      call: () => ({ path: "/api/v2/plan/event", body: { event: { id: real(ev.id), start: to.start, end: to.end } } }),
    });
  },

  /** Delete: scope "occurrence" skips one date of a series, "series" removes the whole event. */
  deleteEvent(ev, scope) {
    const saved = C.entriesOf(S.data.events, ev.id);
    const one = ev.recur && scope === "occurrence";
    enqueue({
      what: "delete",
      apply: (d) => Object.assign({}, d, { events: one ? C.removeEvent(d.events, ev.id, ev.occurrence) : C.removeSeries(d.events, ev.id) }),
      undo: (d) => Object.assign({}, d, { events: C.restoreEvents(d.events, ev.id, saved) }),
      call: () => (one
        ? { path: "/api/v2/plan/event/skip", body: { id: real(ev.id), occurrence: ev.occurrence } }
        : { path: "/api/v2/plan/event/delete", body: { id: real(ev.id) } }),
    });
  },

  /** Manual, optional done mark on one event (or one occurrence of a series). */
  markDone(ev, flag) {
    const saved = C.entriesOf(S.data.events, ev.id);
    enqueue({
      what: "done",
      apply: (d) => Object.assign({}, d, { events: C.setDone(d.events, ev.id, ev.occurrence, flag) }),
      undo: (d) => Object.assign({}, d, { events: C.restoreEvents(d.events, ev.id, saved) }),
      call: () => ({ path: "/api/v2/plan/event/done", body: Object.assign({ id: real(ev.id), done: flag }, ev.recur ? { occurrence: ev.occurrence } : {}) }),
    });
  },

  /** Drop a memo on the calendar: creates its linked event, or moves it when the memo is already scheduled. */
  placeMemo(task, day, startMin, endMin) {
    if (task.sample) { setNotice("Sample data, not saved.", "warn"); return; }
    const start = C.fromMin(startMin), end = C.fromMin(endMin);
    const prev = task.planned || null;
    const linked = S.data.events.filter((e) => e.task_of === task.id).slice(0, 1);
    const tmp = linked.length ? null : newTmp("evt");
    enqueue({
      what: "place",
      apply: (d) => {
        const events = linked.length
          ? d.events.map((e) => (e.id === linked[0].id ? Object.assign({}, e, { day, occurrence: day, start, end }) : e))
          : C.addEvent(d.events, { id: tmp, title: task.title, day, start, end, area: task.area, task_of: task.id });
        return Object.assign({}, d, { events, tasks: C.setPlanned(d.tasks || {}, task.id, { day, start, end, on: "me" }) });
      },
      undo: (d) => Object.assign({}, d, {
        events: linked.length ? C.restoreEvents(d.events, linked[0].id, linked) : d.events.filter((e) => e.id !== tmp),
        tasks: C.setPlanned(d.tasks || {}, task.id, prev),
      }),
      call: () => ({ path: "/api/v2/plan/task/place", body: { id: real(task.id), target: "me", day, start, end } }),
      after: (r) => {
        const id = r.changed && r.changed.id;
        if (tmp && id) { S.idMap[tmp] = id; S.data = Object.assign({}, S.data, { events: C.renameId(S.data.events, tmp, id) }); }
      },
    });
  },

  addTask(f) {
    const tmp = newTmp("t");
    const task = { id: tmp, title: f.title, state: "todo", owner: "me", source: "tasks.yaml", project: null, estimate_min: f.estimate, reversible: true, area: f.area };
    enqueue({
      what: "task",
      apply: (d) => Object.assign({}, d, { tasks: C.addTask(d.tasks || {}, task) }),
      undo: (d) => Object.assign({}, d, { tasks: C.removeTask(d.tasks || {}, tmp) }),
      call: () => ({ path: "/api/v2/plan/task", body: { task: { area: f.area, title: f.title, owner: "me", estimate_min: f.estimate || undefined, state: "todo" } } }),
      after: (r) => {
        const id = r.changed && r.changed.id;
        if (id) { S.idMap[tmp] = id; S.data = Object.assign({}, S.data, { tasks: C.renameTask(S.data.tasks, tmp, id) }); }
      },
    });
  },

  setTaskState(task, state) {
    const prev = task.state;
    enqueue({
      what: "state",
      apply: (d) => Object.assign({}, d, { tasks: C.setTaskState(d.tasks || {}, task.id, state) }),
      undo: (d) => Object.assign({}, d, { tasks: C.setTaskState(d.tasks || {}, task.id, prev) }),
      call: () => ({ path: "/api/v2/plan/task/state", body: { id: real(task.id), state } }),
    });
  },

  notice: (text, kind) => setNotice(text, kind),
};

/** The event as the engine wants it. A cleared optional field is sent as null (removes it). */
function wireEvent(f) {
  const e = { title: f.title, day: f.day, area: f.area || null, notes: f.notes || null };      // an edit never sends `type`: appointment, errand and free keep theirs
  if (f.start === undefined) { /* time untouched: not sent */ }
  else if (f.start != null) { e.start = C.fromMin(f.start); e.end = C.fromMin(f.end); } else { e.start = null; e.end = null; }
  if (f.recur !== undefined) e.recur = f.recur || null;
  Object.keys(e).forEach((k) => { if (e[k] === undefined) delete e[k]; });
  return e;
}

/* ---------- header ---------- */
function buildHead(root) {
  const { ui } = S.ctx;
  const head = el("div", "plan-head", null, root);
  const left = el("div", "plan-head-l", null, head);
  el("div", "eyebrow", "Plan", left);
  S.els.label = el("h1", "plan-label", "", left);
  const right = el("div", "plan-head-r", null, head);
  S.els.seg = ui.segmented(right, {
    options: [{ value: "day", label: "Day" }, { value: "week", label: "Week" }],
    value: S.view,
    onChange: (v) => { S.view = v; paint(); },
  });
  const nav = el("div", "plan-nav", null, right);
  const step = (dir) => { S.day = C.addDays(S.day, dir * (S.view === "week" ? 7 : 1)); navigated(); };
  ui.button(nav, { label: "Previous", onClick: () => step(-1) });
  ui.button(nav, { label: "Today", onClick: () => { S.day = todayISO(); navigated(); } });
  ui.button(nav, { label: "Next", onClick: () => step(1) });
  S.els.loading = el("span", "plan-loading", "", right);
}

function navigated() {
  const range = C.fetchRange(S.day);
  const key = range.from + "/" + range.to;
  if (key !== S.rangeKey) { S.loading = true; refresh(true); }
  paint();
}

/* ---------- paint ---------- */
function paint() {
  if (!S || S.dead || !S.els.main) return;
  S.ctx.keepScroll(() => {
    S.els.label.textContent = C.rangeLabel(S.view, S.day);
    S.els.seg.set(S.view);
    S.els.loading.textContent = S.loading && S.data ? "Loading" : "";
    S.els.errbox.hidden = !S.loadError;
    S.els.errbox.textContent = S.loadError;
    S.els.main.hidden = !S.data;
    if (!S.data) return;
    renderTime(P, S.els.time);
    renderMemos(P);
  });
}

function onPointerUpIdle() {
  if (S && S.stale && !S.pending && !S.drag) refresh(true);
}

const P = {
  get S() { return S; },
  get ctx() { return S.ctx; },
  act,
  el,
  C,
  paint,
  refresh,
  dragEnded: onPointerUpIdle,
  openForm: (o) => openEventForm(P, o),
  closeForm: () => closeForm(P),
  chooser: (o) => openChooser(P, o),
  today: todayISO,
  goToDay(iso, view) { S.day = iso; if (view) S.view = view; navigated(); },
};

export default {
  id: "plan",
  title: "Plan",
  key: "p",
  mount(root, ctx) {
    S = freshState(ctx);
    root.textContent = "";
    const wrap = el("div", "screen-in plan", null, root);
    buildHead(wrap);
    S.els.notice = el("div", "plan-notice", null, wrap);
    S.els.notice.hidden = true;
    S.els.notice.setAttribute("role", "alert");
    S.els.errbox = el("div", "err-line", null, wrap);
    S.els.errbox.hidden = true;
    S.els.main = el("div", "plan-grid", null, wrap);
    const lcol = el("div", "plan-left", null, S.els.main);
    S.els.time = el("section", "card plan-time", null, lcol);
    S.els.rail = el("aside", "card plan-rail", null, S.els.main);
    buildMemoRail(P, S.els.rail);
    S.els.floatHost = el("div", "plan-float-host", null, wrap);
    S.els.main.hidden = true;
    S.onKey = (ev) => { if (ev.key === "Escape") { closeForm(P); } };
    document.addEventListener("keydown", S.onKey);
    ctx.every(15000, () => refresh(false));
    paintNotice();
  },
  unmount() {
    if (!S) return;
    S.dead = true;
    stopFit();
    clearTimeout(S.noticeTimer);
    document.removeEventListener("keydown", S.onKey);
    closeForm(P);
    S = null;
  },
};
