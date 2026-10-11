/* Plan screen: the event form (create, edit, schedule a review) and the "this one / the series" chooser.
   Built from kit/ui controls; the values live in `S.form`, so a poll repaint never touches an open form. */
import {
  buildRecur, EDIT_MIN, EDIT_MAX, SNAP, fromMin, toMin, dayNum, dayFromNum, dayShort, reviewLen, clamp, snap, allDayTitleOn,
} from "./plan-core.js";

function mk(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

const NOW_DAYS_BACK = 366, NOW_DAYS_FWD = 800;

export function closeForm(P) {
  const S = P.S;
  if (!S) return;
  if (S.form && S.form.root) S.form.root.remove();
  if (S.form && S.form.lock) { S.form.lock.style.overflow = S.form.lockWas; }
  const back = S.form && S.form.back;
  S.form = null;
  if (back && back.isConnected && back.focus) back.focus();
  closeChooser(P);
}

function closeChooser(P) {
  const S = P.S;
  if (S && S.chooser) { S.chooser.remove(); S.chooser = null; }
}

/** A small decision box. o = {title, text, options: [{label, run}], onCancel?} */
export function openChooser(P, o) {
  const { S, ctx } = P;
  closeChooser(P);
  const box = mk("div", "plan-chooser", null, S.els.floatHost);
  box.setAttribute("role", "dialog");
  mk("div", "plan-chooser-t", o.title, box);
  if (o.text) mk("div", "plan-chooser-x", o.text, box);
  const row = mk("div", "plan-chooser-b", null, box);
  const done = () => { box.remove(); if (S.chooser === box) S.chooser = null; };
  o.options.forEach((opt) => ctx.ui.button(row, { label: opt.label, onClick: () => { done(); opt.run(); } }));
  ctx.ui.button(row, { label: "Cancel", onClick: () => { done(); if (o.onCancel) o.onCancel(); } });
  S.chooser = box;
}

function nextQuarter() {
  const d = new Date();
  return clamp(snap(d.getHours() * 60 + d.getMinutes() + 7), EDIT_MIN, EDIT_MAX - 60);
}

/** o = {mode: 'new', day, start?, end?} | {mode: 'edit', ev} | {mode: 'review', item, day?} */
export function openEventForm(P, o) {
  const { S, ctx } = P;
  const ui = ctx.ui;
  closeForm(P);
  const today = dayNum(P.today());
  const lo = today - NOW_DAYS_BACK, hi = today + NOW_DAYS_FWD;
  const ev = o.mode === "edit" ? o.ev : null;
  const item = o.mode === "review" ? o.item : null;
  const recurring = !!(ev && ev.recur);
  const day0 = ev ? ev.day : (o.day || S.day);
  let s0 = ev && ev.start ? toMin(ev.start) : o.start;
  let e0 = ev && ev.end ? toMin(ev.end) : o.end;
  if (item) {
    if (item.scheduled && item.scheduled.start) { s0 = toMin(item.scheduled.start); e0 = toMin(item.scheduled.end); }
    else { s0 = o.day && o.day === P.today() ? nextQuarter() : 540; e0 = s0 + reviewLen(item); }
  }
  if (s0 == null) { s0 = 540; e0 = 600; }
  if (e0 == null) e0 = s0 + 60;
  const f = S.form = {
    mode: o.mode, ev, item,
    scope: recurring ? "occurrence" : "series",
    title: item ? "Review: " + item.title : (ev ? ev.title : ""),
    area: item ? (item.area || "") : (ev ? (ev.area || "") : ""),
    day: dayNum(item && item.scheduled ? item.scheduled.day : day0),
    allDay: !!(ev && !ev.start),
    start: clamp(s0, EDIT_MIN, EDIT_MAX - SNAP),
    end: clamp(e0, EDIT_MIN + SNAP, EDIT_MAX),
    notes: ev && ev.notes ? ev.notes : "",
    repeat: ev && ev.recur ? (ev.recur.freq === "weekly" ? "weekly" : ev.recur.freq === "daily" ? "daily" : "other") : "none",
    interval: ev && ev.recur && ev.recur.interval ? ev.recur.interval : 1,
    until: ev && ev.recur && ev.recur.until ? dayNum(ev.recur.until) : null,
    armed: false,
    timeTouched: !recurring,
    repTouched: false,
  };
  const backdrop = mk("div", "plan-form-bd", null, S.els.floatHost);
  const root = mk("div", "plan-form", null, backdrop);
  f.root = backdrop;
  f.back = document.activeElement;
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) closeForm(P); });
  const scr = document.getElementById("screen");        // the page does not scroll behind the drawer
  if (scr) { f.lock = scr; f.lockWas = scr.style.overflow; scr.style.overflow = "hidden"; }
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.tabIndex = -1;
  const head = mk("div", "plan-form-h", null, root);
  mk("b", null, item ? "Schedule review" : ev ? "Edit event" : "New event", head);
  const x = mk("button", "plan-link", "Close", head);
  x.type = "button";
  x.addEventListener("click", () => closeForm(P));

  const body = mk("div", "plan-form-b", null, root);
  const row = (label, cls) => { const r = mk("div", "plan-frow" + (cls ? " " + cls : ""), null, body); mk("span", "plan-fl", label, r); return mk("div", "plan-fc", null, r); };
  const note = (text) => mk("div", "plan-fnote", text, body);

  let scopeSeg = null;
  if (recurring) {
    scopeSeg = ui.segmented(row("Applies to"), {
      options: [{ value: "occurrence", label: "This occurrence" }, { value: "series", label: "The series" }],
      value: f.scope,
      onChange: (v) => { f.scope = v; sync(); },
    });
  }

  let titleF = null, notesF = null, areaDD = null;
  if (item) {
    mk("div", "plan-ftitle", f.title, body);
  } else {
    titleF = ui.field(row("Title"), { label: "", value: f.title, onInput: (v) => { f.title = v; sync(); }, placeholder: "What happens" });
    areaDD = ui.dropdown(row("Area"), {
      items: [{ value: "", label: "No area" }].concat(ctx.areas.map((a) => ({ value: a.id, label: a.name }))),
      value: f.area,
      onChange: (v) => { f.area = v; },
    });
  }

  const dayStep = ui.stepper(row("Day"), {
    value: f.day, min: lo, max: hi, step: 1, format: (v) => dayShort(dayFromNum(v)),
    onChange: (v) => { f.day = v; sync(); },
  });
  const dayNote = mk("div", "plan-fnote", "", body);

  let kindSeg = null;
  if (!item) {
    kindSeg = ui.segmented(row("Time"), {
      options: [{ value: "timed", label: "Timed" }, { value: "all", label: "All day" }],
      value: f.allDay ? "all" : "timed",
      onChange: (v) => { f.timeTouched = true; f.allDay = v === "all"; sync(); },
    });
  }
  const startRow = row("Start", "plan-ftime");
  const startStep = ui.stepper(startRow, {
    value: f.start, min: EDIT_MIN, max: EDIT_MAX - SNAP, step: SNAP, format: fromMin,
    onChange: (v) => { f.timeTouched = true; f.start = v; if (f.end <= v) { f.end = v + SNAP; endStep.set(f.end); } sync(); },
  });
  const endRow = row("End", "plan-ftime");
  const endStep = ui.stepper(endRow, {
    value: f.end, min: EDIT_MIN + SNAP, max: EDIT_MAX, step: SNAP, format: fromMin,
    onChange: (v) => { f.timeTouched = true; f.end = v; if (f.start >= v) { f.start = v - SNAP; startStep.set(f.start); } sync(); },
  });

  let repSeg = null, untilSeg = null, untilStep = null, repRows = [];
  if (!item) {
    notesF = ui.field(row("Notes"), { label: "", value: f.notes, onInput: (v) => { f.notes = v; }, placeholder: "Optional" });
    repSeg = ui.segmented(row("Repeat"), {
      options: [{ value: "none", label: "None" }, { value: "daily", label: "Daily" }, { value: "weekly", label: "Weekly" }].concat(f.repeat === "other" ? [{ value: "other", label: "Keep as is" }] : []),
      value: f.repeat,
      onChange: (v) => { f.repTouched = true; f.repeat = v; sync(); },
    });
    const ur = row("Until");
    untilSeg = ui.segmented(ur, {
      options: [{ value: "never", label: "No end" }, { value: "until", label: "Until" }],
      value: f.until == null ? "never" : "until",
      onChange: (v) => { f.repTouched = true; f.until = v === "never" ? null : Math.max(f.day, f.until || f.day + 28); untilStep.set(f.until || f.day); sync(); },
    });
    untilStep = ui.stepper(ur, {
      value: f.until || f.day, min: lo, max: hi + 400, step: 1, format: (v) => dayShort(dayFromNum(v)),
      onChange: (v) => { f.repTouched = true; f.until = v; },
    });
    repRows = [ur.parentNode];
  }

  const errLine = mk("div", "plan-ferr", "", body);
  const foot = mk("div", "plan-form-f", null, root);
  const save = ui.button(foot, { label: item ? "Schedule" : "Save", onClick: submit });
  ui.button(foot, { label: "Cancel", onClick: () => closeForm(P) });
  let del = null;
  if (ev) {
    del = ui.button(foot, {
      label: "Delete",
      onClick: () => {
        if (recurring) {
          closeForm(P);
          P.chooser({
            title: "Delete recurring event: " + ev.title,
            text: "Remove",
            options: [
              { label: "This occurrence", run: () => P.act.deleteEvent(ev, "occurrence") },
              { label: "The series", run: () => P.act.deleteEvent(ev, "series") },
            ],
          });
        } else if (!f.armed) {
          f.armed = true;
          del.el.textContent = "Confirm delete";
          errLine.textContent = "Click Confirm delete to remove this event.";
        } else {
          closeForm(P);
          P.act.deleteEvent(ev, "series");
        }
      },
    });
  }

  function sync() {
    const timed = !f.allDay;
    const occ = recurring && f.scope === "occurrence";
    startRow.parentNode.hidden = !timed;
    endRow.parentNode.hidden = !timed;
    if (titleF) { titleF.el.disabled = occ; }
    if (notesF) { notesF.el.disabled = occ; }
    if (areaDD) areaDD.el.classList.toggle("dis", occ);
    if (repSeg) repSeg.el.classList.toggle("dis", occ);
    if (untilSeg) untilSeg.el.classList.toggle("dis", occ);
    if (untilStep) untilStep.el.classList.toggle("dis", occ);
    repRows.forEach((r) => { r.hidden = !(f.repeat !== "none"); });
    const fixedDay = recurring && f.scope === "series";
    dayStep.el.classList.toggle("dis", fixedDay);
    const timeNote = recurring && !occ && !f.timeTouched && timed ? " The times shown are this occurrence's; they apply to the series only if changed here." : "";
    dayNote.textContent = (fixedDay ? "The series keeps its start day. Choose This occurrence to move one date." : "") + timeNote;
    dayNote.hidden = !dayNote.textContent;
    let why = "";
    if (!item && !f.title.trim() && !occ) why = "A title is needed.";
    // a day that already has an all-day event takes no new event (the engine refuses it too)
    const dayIso = dayFromNum(f.day);
    const arriving = item ? !(item.scheduled && item.scheduled.day === dayIso) : (!ev || ev.day !== dayIso);
    const blocker = arriving && !occ ? allDayTitleOn(S.data.events, dayIso, ev ? ev.id : null) : "";
    if (!why && blocker) why = dayShort(dayIso) + " already has the all-day event '" + blocker + "'. Choose another day.";
    save.setDisabled(!!why, why);
    if (occ && !errLine.textContent) errLine.textContent = "";
    note_occ.textContent = occ ? "Title, area, notes and repeat belong to the series. Choose The series to change them." : "";
    note_occ.hidden = !occ;
  }
  const note_occ = note("");
  note_occ.hidden = true;

  function submit() {
    const occ = recurring && f.scope === "occurrence";
    const day = dayFromNum(f.day);
    const timed = !f.allDay;
    const vals = { title: f.title.trim(), area: f.area || null, notes: (f.notes || "").trim() || null, day, start: timed ? f.start : null, end: timed ? f.end : null };
    // a series keeps its own times unless they were changed here (the form shows the occurrence's time, which may be an override)
    if (recurring && !occ && !f.timeTouched) { delete vals.start; delete vals.end; vals.start = undefined; vals.end = undefined; }
    if (item) {
      closeForm(P);
      P.act.scheduleReview(item, day, f.start, f.end);
      return;
    }
    if (!vals.title && !occ) return;
    if (repSeg && f.repTouched) {              // untouched Repeat: `recur` is not sent, the stored one stays as is
      if (f.repeat === "none") vals.recur = null;
      else if (f.repeat === "other") vals.recur = ev.recur;
      else {
        vals.recur = buildRecur(ev && ev.recur, f.repeat, f.until != null ? dayFromNum(Math.max(f.until, f.day)) : null, f.interval);
      }
    }
    closeForm(P);
    if (o.mode === "new") P.act.createEvent(vals);
    else if (occ) {
      if (vals.start == null) {
        if (day !== ev.day) P.act.moveEvent(ev, { day, start: null, end: null });
      } else P.act.moveEvent(ev, { day, start: fromMin(f.start), end: fromMin(f.end) });
    } else P.act.editEvent(ev, vals);
  }

  backdrop.addEventListener("keydown", (e) => {      // focus trap: Tab cycles inside the drawer
    if (e.key !== "Tab") return;
    const f2 = Array.from(root.querySelectorAll("button, input, select, textarea, [tabindex]:not([tabindex='-1'])")).filter((n) => !n.disabled && !n.closest("[hidden]") && n.offsetParent !== null);
    if (!f2.length) { e.preventDefault(); root.focus(); return; }
    const first = f2[0], last = f2[f2.length - 1], cur = document.activeElement;
    if (e.shiftKey && (cur === first || cur === root || !root.contains(cur))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (cur === last || !root.contains(cur))) { e.preventDefault(); first.focus(); }
  });
  root.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT" && !save.el.disabled) { e.preventDefault(); submit(); }
  });
  sync();
  setTimeout(() => { if (S.form === f) (titleF && !ev ? titleF.el : root).focus(); }, 0);
}
