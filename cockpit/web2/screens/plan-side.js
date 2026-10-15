/* Plan screen: the "To schedule" memo rail (right column). A memo is a task from tasks.yaml; it is scheduled
   by dragging it onto the calendar (task/place) or from a session (tools/plan_day.py). */
import {
  memoList, memoLen, memoPlannedLabel, isScheduledMemo, fmtEstimate, fmtMemoEst, EDIT_MIN, EDIT_MAX, snap, clamp, fromMin, dayShort, areaVar,
} from "./plan-core.js";
import { slotAt, surfaceRect } from "./plan-time.js";

function mk(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}
function areaCol(P, a) { return areaVar(P.ctx.areas, a); }

/** The rail's frame, built once: heading, help, quick add, list. The add form keeps its values in S.taskForm. */
export function buildMemoRail(P, host) {
  const { S } = P;
  const ch = mk("div", "ch", null, host);
  const ct = mk("div", "ct", null, ch);
  mk("h3", null, "To schedule", ct);
  mk("p", null, "Ideas to schedule later. Drag one onto the calendar, or plan it from a session.", ct);
  S.els.memoAdd = mk("div", "plan-memoadd", null, host);
  buildMemoAdd(P, S.els.memoAdd);
  S.els.memoList = mk("div", "plan-memolist", null, host);
  S.els.memoList.setAttribute("data-keep-scroll", "");
}

export function renderMemos(P) {
  const { S } = P;
  const host = S.els.memoList;
  if (S.memoAddH) S.memoAddH.sync();
  host.textContent = "";
  const memos = memoList(S.data.tasks || {});
  if (!memos.length) {
    mk("div", "plan-rv-empty", "Nothing to schedule. Add an idea above.", host);
    return;
  }
  const shortOf = {};
  (S.data.areas || []).forEach((a) => { shortOf[a.id] = a.short || String(a.id).toUpperCase().slice(0, 5); });
  memos.forEach((t) => {
    const scheduled = isScheduledMemo(t);
    const row = mk("div", "plan-memo" + (scheduled ? " sched" : ""), null, host);
    row.style.setProperty("--c", areaCol(P, t.area));
    const top = mk("div", "plan-memo-t", null, row);
    mk("i", "plan-memo-dot", null, top);
    mk("span", "plan-memo-a mono", shortOf[t.area] || "-", top);
    const title = mk("span", "plan-memo-n", t.title, top);
    title.title = t.title;
    const meta = mk("div", "plan-memo-m", null, row);
    mk("span", "mono", scheduled ? memoPlannedLabel(t.planned) : (fmtMemoEst(t.estimate_min) || "no estimate"), meta);
    const rm = mk("button", "plan-link", "Remove", meta);
    rm.type = "button";
    rm.addEventListener("click", () => P.act.setTaskState(t, "done"));
    row.addEventListener("pointerdown", (e) => {
      if (e.target.closest("button") || (e.pointerType === "mouse" && e.button !== 0)) return;
      cardDrag(P, e, { task: t, title: t.title, len: memoLen(t) }, row);
    });
  });
}

/** Where a dragged memo would land: {day, start, end} | null. */
function dropTarget(P, ev, drag) {
  const len = drag.len;
  const mine = slotAt(P, ev.clientX, ev.clientY);
  if (mine) {
    const start = clamp(snap(mine.min - len / 2), EDIT_MIN, Math.max(EDIT_MIN, EDIT_MAX - len));
    return { day: mine.day, start, end: Math.min(start + len, EDIT_MAX) };
  }
  return null;
}

/** Drag a memo card onto the calendar. */
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
      S.drag = { memo: true };
      P.ctx.kit.hideTip();
      ghost = mk("div", "plan-ghost", null, document.body);
      mk("b", null, drag.title, ghost);
      tip = mk("span", null, "", ghost);
      card.classList.add("lift");
    }
    ev.preventDefault();
    place(ev);
    const t = dropTarget(P, ev, drag);
    if (t) tip.textContent = dayShort(t.day) + " " + fromMin(t.start) + " to " + fromMin(t.end);
    else tip.textContent = "Drop on the calendar";
    light(t ? (S.surface && S.surface.el) : null);
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
    if (t) P.act.placeMemo(drag.task, t.day, t.start, t.end);
    else if (!cancelled) P.act.notice("Drop the memo on the calendar.", "warn");
    P.dragEnded();
  };
  const onUp = (ev) => finish(ev, false);
  const onCancel = (ev) => finish(ev, true);
  document.addEventListener("pointermove", onMove);
  document.addEventListener("pointerup", onUp);
  document.addEventListener("pointercancel", onCancel);
}

/** The quick-add block: built once, its values live in S.taskForm. A new memo is a task of mine, state todo. */
function buildMemoAdd(P, host) {
  const { S, ctx } = P;
  const ui = ctx.ui;
  const f = S.taskForm;
  const titleF = ui.field(host, { label: "Title", value: f.title, onInput: (v) => { f.title = v; sync(); }, placeholder: "New memo" });
  const opts = mk("div", "plan-memoadd-o", null, host);
  const areaWrap = mk("div", "plan-ta-c", null, opts);
  mk("span", "plan-fl", "Area", areaWrap);
  const areaDD = ui.dropdown(areaWrap, { items: [], value: f.area, onChange: (v) => { f.area = v; sync(); } });
  const estWrap = mk("div", "plan-ta-c", null, opts);
  mk("span", "plan-fl", "Estimate", estWrap);
  ui.stepper(estWrap, { value: f.estimate, min: 0, max: 960, step: 15, format: (v) => (v ? fmtEstimate(v) : "none"), onChange: (v) => { f.estimate = v; } });
  const add = ui.button(host, {
    label: "Add",
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
  S.memoAddH = { sync };
  sync();
}
