/* Plan screen: the review tray (right column) and tasks per area. */
import {
  STATES, STATE_LABELS, groupTasks, isReadOnlyTask, fmtEstimate, reviewLen, EDIT_MIN, EDIT_MAX, snap, clamp, fromMin, dayShort,
  taskLen, plannedLabel, placeReason, tasksByState,
} from "./plan-core.js";
import { slotAt, surfaceRect } from "./plan-time.js";

function mk(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}
function areaCol(a) { return a ? "var(--a-" + a + ")" : "var(--ctx-2)"; }

/* ---------- review tray ---------- */
export function renderTray(P, host) {
  const { S, ctx } = P;
  host.textContent = "";
  const tray = S.data.review_tray || [];
  const ch = mk("div", "ch", null, host);
  const ct = mk("div", "ct", null, ch);
  mk("h3", null, "Review tray", ct);
  mk("p", null, tray.length + (tray.length === 1 ? " item" : " items") + " waiting. Drag one onto the calendar to schedule it.", ct);
  const list = mk("div", "plan-rvlist", null, host);
  list.setAttribute("data-keep-scroll", "");
  if (!tray.length) {
    mk("div", "plan-rv-empty", "No finished work is waiting for review.", list);
    return;
  }
  tray.forEach((it) => {
    const col = areaCol(it.area);
    const card = mk("div", "plan-rv", null, list);
    card.style.setProperty("--c", col);
    mk("i", "plan-rv-bar", null, card);
    const bodyEl = mk("div", "plan-rv-b", null, card);
    mk("div", "plan-rv-t", it.title, bodyEl);
    const meta = mk("div", "plan-rv-m", null, bodyEl);
    mk("span", "mono", "est. " + (it.review_min || 20) + " min", meta);
    mk("span", "plan-rv-v mono", it.verdict || "", meta);
    if (it.scheduled && it.scheduled.day) {
      mk("div", "plan-rv-s", "Scheduled " + dayShort(it.scheduled.day) + " " + it.scheduled.start + " to " + it.scheduled.end, bodyEl);
    }
    const acts = mk("div", "plan-rv-a", null, bodyEl);
    ctx.ui.button(acts, { label: it.scheduled ? "Reschedule" : "Schedule", onClick: () => P.openForm({ mode: "review", item: it, day: S.day }) });
    ctx.ui.button(acts, { label: "Mark reviewed", onClick: () => P.act.reviewDone(it) });
    card.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button") || (e.pointerType === "mouse" && e.button !== 0)) return;
      cardDrag(P, e, { item: it, title: it.title, len: reviewLen(it) }, card);
    });
  });
}

/** Where a dragged card would land: {kind: "me", day, start, end} | {kind: "none", why} | null. */
function dropTarget(P, ev, drag) {
  const { S } = P;
  const len = drag.len;
  const mine = slotAt(P, ev.clientX, ev.clientY);
  if (mine) {
    const start = clamp(snap(mine.min - len / 2), EDIT_MIN, EDIT_MAX - len);
    return { kind: "me", day: mine.day, start, end: start + len };
  }
  return null;
}

/** Drag a card (a review or a task) onto the calendar. */
function cardDrag(P, e0, drag, card) {
  const { S } = P;
  if (S.drag) return;
  let moved = false, ghost = null, tip = null, lit = null;
  const place = (ev) => {
    ghost.style.left = (ev.clientX + 12) + "px";
    ghost.style.top = (ev.clientY + 12) + "px";
  };
  const light = (el) => {
    if (lit && lit !== el) lit.classList.remove("drop");
    lit = el;
    if (lit) lit.classList.add("drop");
  };
  const onMove = (ev) => {
    if (!moved) {
      if (Math.hypot(ev.clientX - e0.clientX, ev.clientY - e0.clientY) < 5) return;
      moved = true;
      S.drag = { tray: true };
      P.ctx.kit.hideTip();
      ghost = mk("div", "plan-rv-ghost", null, document.body);
      mk("b", null, drag.title, ghost);
      tip = mk("span", null, "", ghost);
      card.classList.add("lift");
    }
    ev.preventDefault();
    place(ev);
    const t = dropTarget(P, ev, drag);
    if (t && t.kind === "none") tip.textContent = t.why;
    else if (t) tip.textContent = dayShort(t.day) + " " + fromMin(t.start) + " to " + fromMin(t.end);
    else tip.textContent = "Drop on the calendar";
    light(t && t.kind === "me" ? (S.surface && S.surface.el) : null);
  };
  const finish = (ev, cancelled) => {
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", onUp);
    document.removeEventListener("pointercancel", onCancel);
    if (!moved) return;
    S.drag = null;
    if (ghost) ghost.remove();
    card.classList.remove("lift");
    light(null);
    const t = !cancelled && surfaceRect(P) ? dropTarget(P, ev, drag) : null;
    if (t && t.kind === "none") P.act.notice("Not planned: " + t.why, "warn");
    else if (t && drag.task) P.act.placeTask(drag.task, t.day, t.start, t.end);
    else if (t) P.act.scheduleReview(drag.item, t.day, t.start, t.end);
    else if (!cancelled) P.act.notice(drag.task ? "Drop the task on a free slot in the calendar." : "Drop the review on a free slot in the calendar.", "warn");
    P.dragEnded();
  };
  const onUp = (ev) => finish(ev, false);
  const onCancel = (ev) => finish(ev, true);
  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
  document.addEventListener("pointercancel", onCancel);
}

/* ---------- tasks: group by area or by state ---------- */
/** "Group by: Area | State" and the area chips (themed toggles). Built once; values live in S. */
export function buildTaskControls(P, host) {
  const { S, ctx } = P;
  const left = mk("div", "plan-ta-c", null, host);
  mk("span", "plan-fl", "Group by", left);
  ctx.ui.segmented(left, {
    options: [{ value: "area", label: "Area" }, { value: "state", label: "State" }],
    value: S.taskGroup,
    onChange: (v) => { S.taskGroup = v; P.paint(); },
  });
  const right = mk("div", "plan-ta-c plan-chips-w", null, host);
  mk("span", "plan-fl", "Areas shown", right);
  const box = mk("div", "plan-chips", null, right);
  let seen = "";
  const sync = () => {
    const key = ctx.areas.map((a) => a.id).join(",");
    if (key !== seen) {
      seen = key;
      box.textContent = "";
      ctx.areas.forEach((a) => {
        const b = mk("button", "plan-achip", null, box);
        b.type = "button";
        b.dataset.area = a.id;
        mk("i", "sw", null, b).style.background = areaCol(a.id);
        mk("span", null, a.name, b);
        b.addEventListener("click", () => { S.hiddenAreas[a.id] = !S.hiddenAreas[a.id]; P.paint(); });
      });
    }
    box.querySelectorAll(".plan-achip").forEach((b) => {
      const hidden = !!S.hiddenAreas[b.dataset.area];
      b.classList.toggle("off", hidden);
      b.setAttribute("aria-pressed", hidden ? "false" : "true");
      b.title = (hidden ? "Show " : "Hide ") + (ctx.areaName ? ctx.areaName(b.dataset.area) : b.dataset.area) + " tasks";
    });
  };
  S.taskCtlH = { sync };
  sync();
}

export function renderTasks(P, host) {
  const { S, ctx } = P;
  if (host.querySelector(".ui-dd.open")) return;       // an open menu must not be torn down by a poll
  host.textContent = "";
  if (S.taskAddH) S.taskAddH.sync();
  if (S.taskCtlH) S.taskCtlH.sync();
  const tasks = S.data.tasks || {};
  const areas = ctx.areas;
  host.classList.toggle("by-state", S.taskGroup === "state");
  if (!areas.length) { mk("div", "plan-rv-empty", "No areas loaded.", host); return; }
  const shown = areas.filter((a) => !S.hiddenAreas[a.id]);
  if (!shown.length) { mk("div", "plan-rv-empty", "Every area is hidden. Turn one on above.", host); return; }
  if (S.taskGroup === "state") { renderByState(P, host, tasks, areas); return; }
  shown.forEach((a) => {
    const list = tasks[a.id] || [];
    const g = groupTasks(list);
    const col = mk("div", "plan-acol", null, host);
    col.style.setProperty("--c", areaCol(a.id));
    const hd = mk("div", "plan-acol-h", null, col);
    mk("i", "sw", null, hd).style.background = areaCol(a.id);
    mk("b", null, a.name, hd);
    mk("span", "mono muted", String(list.filter((t) => t.state !== "done").length), hd);
    if (!list.length) mk("div", "plan-acol-empty", "No tasks.", col);
    STATES.forEach((st) => {
      const items = g[st];
      if (!items.length) return;
      const grp = mk("div", "plan-grp", null, col);
      const gh = mk("div", "plan-grp-h", null, grp);
      mk("span", null, STATE_LABELS[st], gh);
      mk("span", "mono", String(items.length), gh);
      if (st === "done") {
        const open = !!S.doneOpen[a.id];
        const tg = mk("button", "plan-link", open ? "Hide" : "Show", gh);
        tg.type = "button";
        tg.addEventListener("click", () => { S.doneOpen[a.id] = !open; P.paint(); });
        if (!open) return;
      }
      items.forEach((t) => taskRow(P, grp, Object.assign({ area: a.id }, t), false));
    });
  });
}

function renderByState(P, host, tasks, areas) {
  const { S } = P;
  const g = tasksByState(tasks, areas, S.hiddenAreas);
  STATES.forEach((st) => {
    const items = g[st];
    const col = mk("div", "plan-scol", null, host);
    const hd = mk("div", "plan-acol-h", null, col);
    mk("b", null, STATE_LABELS[st], hd);
    mk("span", "mono muted", String(items.length), hd);
    if (st === "done" && items.length) {
      const open = !!S.doneOpen["*"];
      const tg = mk("button", "plan-link", open ? "Hide" : "Show", hd);
      tg.type = "button";
      tg.addEventListener("click", () => { S.doneOpen["*"] = !open; P.paint(); });
      if (!open) return;
    }
    if (!items.length) mk("div", "plan-acol-empty", "No tasks.", col);
    items.forEach((t) => taskRow(P, col, t, true));
  });
}

function taskRow(P, parent, t, withArea) {
  const { ctx } = P;
  const draggable = t.state !== "done";
  const row = mk("div", "plan-task" + (t.state === "done" ? " done" : "") + (draggable ? " drag" : ""), null, parent);
  row.style.setProperty("--c", areaCol(t.area));
  const top = mk("div", "plan-task-t", t.title, row);
  top.title = t.title;
  const meta = mk("div", "plan-task-m", null, row);
  const bits = [];
  if (t.estimate_min != null) bits.push(fmtEstimate(t.estimate_min));
  bits.push(t.owner === "agents" ? "agents" : "me");
  if (t.reversible === false) bits.push("irreversible");
  if (withArea) bits.push(ctx.areaName ? ctx.areaName(t.area) : t.area);
  mk("span", "mono", bits.join(" · "), meta);
  const pl = plannedLabel(t.planned);
  if (pl) {
    const p = mk("span", "plan-planned", pl, meta);
    p.title = t.planned.on === "agents" ? "A slot saved before the agents' lane was parked. Nothing starts by itself." : "Planned in the calendar.";
  }
  if (isReadOnlyTask(t)) {
    mk("span", "plan-ro", "Read-only" + (t.project ? ", " + t.project : ", project plan"), meta);
  } else {
    ctx.ui.dropdown(meta, {
      items: STATES.map((s) => ({ value: s, label: STATE_LABELS[s] })),
      value: t.state,
      onChange: (v) => { if (v !== t.state) P.act.setTaskState(t, v); },
    });
  }
  if (draggable) {
    row.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button, .ui-dd") || (e.pointerType === "mouse" && e.button !== 0)) return;
      cardDrag(P, e, { task: t, title: t.title, len: taskLen(t) }, row);
    });
  }
}

/** The "add a task" row: built once, its values live in S.taskForm. */
export function buildTaskAdd(P, host) {
  const { S, ctx } = P;
  const ui = ctx.ui;
  const f = S.taskForm;
  const titleF = ui.field(host, { label: "Title", value: f.title, onInput: (v) => { f.title = v; sync(); }, placeholder: "New task" });
  const areaWrap = mk("div", "plan-ta-c", null, host);
  mk("span", "plan-fl", "Area", areaWrap);
  const areaDD = ui.dropdown(areaWrap, { items: [], value: f.area, onChange: (v) => { f.area = v; sync(); } });
  const estWrap = mk("div", "plan-ta-c", null, host);
  mk("span", "plan-fl", "Estimate", estWrap);
  ui.stepper(estWrap, { value: f.estimate, min: 0, max: 960, step: 15, format: (v) => (v ? fmtEstimate(v) : "none"), onChange: (v) => { f.estimate = v; } });
  const add = ui.button(host, {
    label: "Add task",
    onClick: () => {
      if (!f.title.trim() || !f.area) return;
      P.act.addTask({ title: f.title.trim(), area: f.area, estimate: f.estimate, owner: "me" });
      f.title = "";
      titleF.set("");
      sync();
    },
  });
  let seen = "";
  function sync() {
    const items = ctx.areas.map((a) => ({ value: a.id, label: a.name }));
    const key = items.map((i) => i.value).join(",");
    if (key !== seen) {
      seen = key;
      if (!f.area && items.length) f.area = items[0].value;
      areaDD.setItems(items);
      areaDD.set(f.area);
    }
    const why = !f.title.trim() ? "A title is needed." : !f.area ? "An area is needed." : "";
    add.setDisabled(!!why, why);
  }
  S.taskAddH = { sync };
  sync();
}
