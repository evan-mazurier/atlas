/* Brief screen (docs/v2/contracts.md §8.6), fed by GET /api/v2/plan/brief (§5.3a). Polled every 60 s; the
   page repaints only when the JSON changed (scroll kept). Writes carry `base`, the Plan version seen with the
   last load; a 409 reloads and shows a notice. Markup and text rules: brief-model.js. */
import * as C from "./plan-core.js";
import { screenHTML, summaryLine, dayTitle, snoozeTargets, snoozeBase, itemKey, todayKey } from "./brief-model.js";

let S = null;

function el(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

function todayISO() { return C.isoOf(new Date()); }

/* ---------- loading ---------- */
async function load(force) {
  if (!S || S.dead || S.busy) return;
  const day = todayISO();
  let b;
  try {
    b = await S.ctx.api.get("/api/v2/plan/brief?day=" + day);
  } catch (e) {
    if (!S || S.dead) return;
    if (!S.data) S.loadError = "Brief unavailable (" + (e && e.message || e) + ").";
    else setNotice("Brief not refreshed: " + (e && e.message || e), "warn");
    paint();
    return;
  }
  if (!S || S.dead || S.busy) return;
  S.loadError = "";
  const sig = JSON.stringify(b);
  const base = typeof b.version === "number" ? b.version : S.base;
  S.base = base;
  if (!force && sig === S.sig) return;
  S.sig = sig;
  S.data = b;
  paint();
}

/* ---------- notice ---------- */
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

/* ---------- writes ---------- */
async function write(path, body, key) {
  if (!S || S.busy) return;
  const s = S;
  if (typeof s.base !== "number") { setNotice("Not saved: the Plan version is not known yet.", "err"); return; }
  s.busy = key;
  delete s.errors[key];
  s.snooze = null;
  paint();
  try {
    await s.ctx.api.post(path, Object.assign({}, body, { base: s.base }));
  } catch (e) {
    if (S !== s) return;
    if (e && e.status === 409) setNotice("Changed elsewhere: reloaded.", "warn");
    else s.errors[key] = "Not saved: " + (e && e.message || e);
  } finally {
    if (S === s) {
      s.busy = null;
      await load(true);
    }
  }
}

function findDeadline(key) {
  const d = S.data;
  return d && d.do_not_miss.concat(d.next).find((it) => itemKey(it) === key);
}
function findToday(key) { return S.data && S.data.today.find((t) => todayKey(t) === key); }

function deadlineBody(it) {
  const b = { id: it.id };
  if (it.recurring) b.occurrence_key = it.occurrence_key;
  return b;
}

const act = {
  done(key) {
    const it = findDeadline(key);
    if (it) write("/api/v2/plan/deadline/done", deadlineBody(it), key);
  },
  move(key, to) {
    const it = findDeadline(key);
    if (it) write("/api/v2/plan/deadline/move", Object.assign(deadlineBody(it), { to_day: to }), key);
  },
  toggle(key) {
    const t = findToday(key);
    if (!t || !t.can_mark_done) return;
    write("/api/v2/plan/event/done", Object.assign({ id: t.id, done: !t.done }, t.occurrence ? { occurrence: t.occurrence } : {}), key);
  },
};

/* ---------- paint ---------- */
function view() {
  return { areaColor: S.ctx.areaColor, expanded: S.expanded, snooze: S.snooze, busy: S.busy, errors: S.errors };
}

function paint() {
  if (!S || S.dead) return;
  S.ctx.keepScroll(() => {
    S.els.err.hidden = !S.loadError;
    S.els.err.textContent = S.loadError;
    S.els.main.hidden = !S.data;
    if (!S.data) return;
    S.els.label.textContent = dayTitle(S.data.day);
    S.els.sum.textContent = summaryLine(S.data.counts);
    S.els.main.innerHTML = screenHTML(S.data, view());
    mountSnooze();
  });
}

/** The Snooze menu of the open row: themed buttons, plus a themed day stepper for "Pick a day". */
function mountSnooze() {
  if (!S.snooze) return;
  const host = S.els.main.querySelector('.brief-snz[data-key="' + S.snooze.replace(/["\\]/g, "\\$&") + '"]');
  if (!host) { S.snooze = null; return; }
  const { ui } = S.ctx;
  const key = S.snooze;
  const day = S.data.day;
  const it = findDeadline(key);
  const from = snoozeBase(it && it.day, day);
  snoozeTargets(from, C.addDays).forEach(([label, to]) => ui.button(host, { label, onClick: () => act.move(key, to) }));
  ui.button(host, { label: "Pick a day", onClick: () => { S.pick = !S.pick; paint(); } });
  if (S.pick) {
    const lo = C.dayNum(day);
    if (S.pickDay == null || S.pickDay < lo) S.pickDay = C.dayNum(from) + 1;
    const row = el("div", "brief-pick", null, host);
    ui.stepper(row, { value: S.pickDay, min: lo, max: lo + 730, step: 1, format: (v) => C.dayShort(C.dayFromNum(v)), onChange: (v) => { S.pickDay = v; } });
    ui.button(row, { label: "Move", onClick: () => act.move(key, C.dayFromNum(S.pickDay)) });
  }
}

function onClick(ev) {
  const b = ev.target.closest("[data-act]");
  if (!b || !S || !S.els.main.contains(b)) return;
  const key = b.dataset.key;
  switch (b.dataset.act) {
    case "done": act.done(key); break;
    case "toggle": act.toggle(key); break;
    case "snooze": S.snooze = S.snooze === key ? null : key; S.pick = false; S.pickDay = null; paint(); break;
    case "plan": { const it = findDeadline(key); if (it) location.hash = "#/plan?day=" + it.day; break; }
    case "open": location.hash = "#/" + b.dataset.to; break;
    case "more": S.expanded[b.dataset.block] = true; paint(); break;
    default: break;
  }
}

export default {
  id: "brief",
  title: "Brief",
  key: "b",
  mount(root, ctx) {
    S = {
      ctx, dead: false, data: null, sig: "", base: null, loadError: "", busy: null, errors: {}, snooze: null, pick: false,
      pickDay: null, expanded: {}, notice: null, noticeTimer: 0, els: {},
    };
    root.textContent = "";
    const wrap = el("div", "screen-in brief", null, root);
    const head = el("div", "brief-head", null, wrap);
    el("div", "eyebrow", "Brief", head);
    S.els.label = el("h1", "brief-label", "", head);
    S.els.sum = el("div", "brief-sum", "", head);
    S.els.notice = el("div", "plan-notice", null, wrap);
    S.els.notice.hidden = true;
    S.els.notice.setAttribute("role", "alert");
    S.els.err = el("div", "err-line", null, wrap);
    S.els.err.hidden = true;
    S.els.main = el("div", "brief-main", null, wrap);
    S.els.main.hidden = true;
    S.els.main.addEventListener("click", onClick);
    ctx.every(60000, () => load(false));
  },
  unmount() {
    if (!S) return;
    S.dead = true;
    clearTimeout(S.noticeTimer);
    S = null;
  },
};
