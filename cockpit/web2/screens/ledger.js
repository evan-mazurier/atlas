/* Ledger screen (docs/v2/contracts.md §8.5), fed by /api/v2/ledger/* (§5.2).
   `?sample=1` (page query or hash query) reads cockpit/web2/sample/ledger-*.json instead, so the
   screen can be checked before the API exists. Filters live in module state; polls never reset them. */
import {
  compact, dayLabel, dayTitle, delta, duration, esc, rosterName, helperTier, isoToHM, isoWeek, niceTicks, tickDecimals,
  shiftWeek, weekLabel,
} from "../kit/format.js";
import { renderMarkdown } from "../kit/markdown.js";
import { bypassListHTML, wireBypassList } from "./ledger-bypass.js";
import { TASK_HEAD_TIPS, roundsParts, statusWord, taskCellParts, taskTitle, verdictParts } from "./ledger-model.js";
import { integrityLabel, integrityLevel, integrityPanelHTML } from "./ledger-integrity.js";

let S = null;
let onKey = null;

const TIER_NAMES = { opus: "Opus", sonnet: "Sonnet", haiku: "Haiku" };
const TYPES = [["", "All types"], ["coding", "Coding"], ["research", "Research"], ["writing", "Writing"], ["chore", "Chore"],
  ["legal", "Legal or money"], ["quick-question", "Quick question"], ["playbook", "Playbook"], ["undeclared", "Undeclared"]];
const STATUSES = [["", "All statuses"], ["open", "In progress"], ["done", "Closed"], ["switched", "Next task started"], ["ended", "Session ended"]];
const KPIS = [
  { key: "tasks_closed", label: "Tasks closed", fmt: (v) => String(v), good: "up", unit: "" },
  { key: "first_pass_ready_pct", label: "First-pass READY", fmt: (v) => v + " %", good: "up", unit: "pts" },
  { key: "caught", label: "Caught by the checks", sub: "findings raised by reviewers", fmt: (v) => String(v), good: "none", unit: "" },
  { key: "rework_refused", label: "Rework refused", fmt: (v) => String(v), good: "down", unit: "" },
  { key: "weighted_usage", label: "Weighted usage", sub: "price-weighted tokens, Sonnet = 1", fmt: compact, good: "down", unit: "%rel" },
];

function el(tag, cls, text, parent) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

function sampleFlag(ctx) {
  const h = location.hash.split("?")[1] || "";
  return ctx.query.get("sample") === "1" || new URLSearchParams(h).get("sample") === "1";
}

async function fetchLedger(ctx, kind, params) {
  if (S.sample) {
    const r = await fetch("/sample/ledger-" + kind + ".json", { cache: "no-store" });
    if (!r.ok) throw new Error("sample file missing (" + kind + ")");
    return r.json();
  }
  const qs = new URLSearchParams();
  Object.entries(params || {}).forEach(([k, v]) => { if (v !== "" && v != null) qs.set(k, v); });
  const q = qs.toString();
  return ctx.api.get("/api/v2/ledger/" + kind + (q ? "?" + q : ""));
}

function card(parent, title, sub, cls) {
  const c = el("div", "card" + (cls ? " " + cls : ""), null, parent);
  const ch = el("div", "ch", null, c);
  const ct = el("div", "ct", null, ch);
  el("h3", null, title, ct);
  const p = el("p", null, sub, ct);
  const lg = el("div", "lg", null, ch);
  return { c, p, lg, ch };
}

function tierLegend(host, kit) {
  kit.legend(host, ["opus", "sonnet", "haiku"].map((m) => ({ name: TIER_NAMES[m], color: "var(--m-" + m + ")" })), "sq");
}

function renderKpis(kit, stats, host) {
  const box = el("div", "kpis", null, host);
  box.style.marginTop = "16px";
  KPIS.forEach((k) => {
    const d = (stats.kpis || {})[k.key] || { value: 0, prev: 0, spark: [] };
    const t = el("div", "kpi", null, box);
    el("div", "kl", k.label, t);
    if (k.sub) el("div", "ledger-ksub", k.sub, t);
    el("div", "kv", d.value == null ? "–" : k.fmt(d.value), t);
    const dd = el("div", "kd", null, t);
    const de = d.value == null || d.prev == null ? { text: "", tone: "neu" } : delta(d.value, d.prev, k.good, k.unit);
    const f = el("span", null, de.text, dd);
    f.style.color = de.tone === "ok" ? "var(--ok)" : de.tone === "bad" ? "var(--crit)" : "var(--ink-2)";
    el("em", null, de.text ? " vs last week" : "vs last week: no data", dd);
    if (k.key === "rework_refused" && d.split) {
      t.title = "Enforced " + d.split.enforced + ", would have been refused " + d.split.would;
      el("div", "kd", "enforced " + d.split.enforced + " · logged " + d.split.would, t).style.color = "var(--muted)";
    }
    kit.spark(t, (d.spark || []).filter((v) => v != null));
  });
}

function renderStats(ctx, stats) {
  const { kit } = ctx;
  const host = S.statsEl;
  host.textContent = "";
  const grid = el("div", "grid", null, host);

  const k = card(grid, "This week", "Against last week, with the last 12 weeks behind each figure", "wide");
  renderKpis(kit, stats, k.c);

  const days = stats.usage_by_day || [];
  const u = card(grid, "Usage per day, by model", days.length ? dayLabel(days[0].day) + " to " + dayLabel(days[days.length - 1].day) + ", weighted tokens" : "Weighted tokens");
  tierLegend(u.lg, kit);
  const ub = el("div", "cb", null, u.c);
  if (days.length) {
    const tot = Math.max(...days.map((d) => (d.opus || 0) + (d.sonnet || 0) + (d.haiku || 0)));
    const ticks = niceTicks(tot / 1e6, 3);
    const step = ticks.length > 1 ? ticks[1] : 1;
    kit.mount(ub, (b, w) => kit.columnChart(b, w, {
      h: 210, mode: "stack", cats: days.map((d) => dayLabel(d.day)), xlabels: days.map((d) => dayLabel(d.day)), titles: days.map((d) => dayTitle(d.day)),
      xEvery: 2, labelW: 46, ticks, fmtTick: (t) => (t === 0 ? "0" : t.toFixed(tickDecimals(step)) + "M"), fmtTip: (v) => v.toFixed(2) + "M",
      series: ["opus", "sonnet", "haiku"].map((m) => ({ name: TIER_NAMES[m], color: "var(--m-" + m + ")", vals: days.map((d) => (d[m] || 0) / 1e6) })),
    }));
  }

  const areas = (stats.time_by_area || []).filter((a) => a.hours > 0);
  const total = areas.reduce((a, d) => a + d.hours, 0);
  const t = card(grid, "Claude's working time, by area", "This week, " + Math.round(total * 10) / 10 + " h in total");
  if (areas.length) {
    kit.stackBar(t.c, areas.map((a) => ({ key: a.area, name: ctx.areaName(a.area), value: a.hours, color: ctx.areaColor(a.area) })), (h) => Math.round(h * 10) / 10 + " h");
  } else el("p", "note", "No time recorded this week.", t.c).style.marginTop = "14px";

  const rh = stats.rounds_hist || {};
  const r = card(grid, "Review rounds before READY", "Last 4 weeks, number of tasks");
  const rb = el("div", "cb", null, r.c);
  const rv = [rh["0"] || 0, rh["1"] || 0, rh["2"] || 0, rh.refused || 0];
  kit.mount(rb, (b, w) => kit.columnChart(b, w, {
    h: 190, mode: "single", padT: 22, cats: ["0", "1", "2", "Refused"], xlabels: ["0", "1", "2", "Refused"],
    titles: ["0 rounds", "1 round", "2 rounds", "Refused"], xEvery: 1, labelW: 50, ticks: niceTicks(Math.max(...rv, 1), 3),
    fmtTick: String, fmtTip: String, fmtCap: String, unit: "tasks", series: [{ name: "Tasks", color: "var(--data)", vals: rv }],
  }));

  const hel = (stats.usage_by_helper || []).slice().sort((a, b) => b.weighted - a.weighted);
  const h = card(grid, "Usage by helper, this week", "Weighted tokens, coloured by model tier");
  tierLegend(h.lg, kit);
  const hbox = el("div", "hbs", null, h.c);
  if (hel.length) {
    kit.hbars(hbox, hel.map((x) => ({ label: rosterName(x.helper), sub: TIER_NAMES[x.tier] || x.tier, v: x.weighted, text: compact(x.weighted), color: "var(--m-" + (x.tier || helperTier(x.helper)) + ")" })), hel[0].weighted);
  } else el("p", "note", "No helper usage recorded.", hbox);

  const ubt = (stats.usage_by_type || []).filter((x) => x.weighted > 0);
  const ut = card(grid, "Usage by type of work, this week", "Weighted tokens per type of work and playbook");
  const ubox = el("div", "hbs", null, ut.c);
  if (ubt.length) {
    kit.hbars(ubox, ubt.map((x) => ({
      label: x.label, sub: x.kind === "playbook" ? "Playbook" : "", v: x.weighted, text: compact(x.weighted),
      color: x.kind === "undeclared" ? "var(--ctx-1)" : "var(--data)", tipName: x.tasks + (x.tasks === 1 ? " task" : " tasks"),
    })), Math.max(...ubt.map((x) => x.weighted)));
  } else el("p", "note", "No usage recorded this week.", ubox);

  const fp = stats.first_pass_by_week || [];
  const f = card(grid, "First-pass READY rate", "Last " + fp.length + " weeks");
  const fb = el("div", "cb", null, f.c);
  const fpv = fp.filter((x) => x.pct != null);       // a week with no data is skipped, never plotted as 0
  if (fpv.length) {
    const last = fp[fp.length - 1].pct;
    kit.mount(fb, (b, w) => kit.lineChart(b, w, {
      h: 170, ticks: [0, 50, 100], zero: 0, fmtTick: (x) => x + " %", labels: fpv.map((x) => String(x.week).replace(/^\d{4}-/, "")), labelW: 36, padR: 56,
      series: [{ name: "First-pass READY", color: "var(--data)", vals: fpv.map((x) => x.pct) }], wash: true, fmtTip: (x) => x + " %", endLabels: [last == null ? "–" : last + " %"],
    }));
  }

  const lad = card(grid, "The ladder", "How much room each type of work has, and what would move it", "wide");
  const lh = el("div", "tscroll", null, lad.c);
  lh.style.marginTop = "8px";
  kit.table(lh, [{ t: "Type" }, { t: "Rung", tip: "how much the type runs on its own: 1 every answer read, 2 reviewed before done, 3 closes itself" }, { t: "Rework budget", num: true }, { t: "Tasks (4 wk)", num: true }, { t: "First-pass READY", num: true }, { t: "What would move it", mu: true }],
    (stats.ladder || []).map((x) => [x.label || x.type, x.rung || "", x.budget == null ? "–" : String(x.budget), String(x.tasks_4w), x.first_pass_pct == null ? "–" : kit.meterCell(x.first_pass_pct), x.move || ""]));

  const by = stats.bypass || { count: 0, blocked: 0, recent: [] };
  const bp = card(grid, "Bypass attempts this week: " + by.count + (by.count === by.blocked ? " (all blocked)" : " (" + by.blocked + " blocked)"),
    "Shell commands that named the ledger or the hooks folder, refused by the hooks", "wide");
  if (by.recent && by.recent.length) {
    const ul = el("ul", "ledger-list ledger-bplist", null, bp.c);
    ul.style.marginTop = "10px";
    ul.innerHTML = bypassListHTML(by.recent, S.bp);           // every dynamic value is escaped inside
    wireBypassList(ul, S.bp);
  } else el("p", "note", "None recorded.", bp.c).style.marginTop = "10px";

  S.unverified = stats.unverified || 0;
  S.integrity = stats.integrity || null;
  paintUnverified();
  paintIntegrity();
}

function paintIntegrity() {
  if (!S || !S.intEl) return;
  const i = S.integrity;
  S.intEl.hidden = !i;
  if (!i) { S.intPanel.hidden = true; return; }
  const lv = integrityLevel(i);
  S.intEl.textContent = integrityLabel(i);
  S.intEl.classList.toggle("ledger-int-bad", lv === "bad");
  S.intEl.classList.toggle("ledger-int-warn", lv === "warn");
  S.intEl.setAttribute("aria-expanded", S.intOpen ? "true" : "false");
  S.intPanel.hidden = !S.intOpen;
  if (S.intOpen) S.intPanel.innerHTML = '<div class="ch"><div class="ct"><h3>Ledger integrity</h3><p>Breaks in the hash chain, with an automatic reading of each</p></div></div>' + integrityPanelHTML(i);
}

function paintUnverified() {
  if (!S || !S.unvEl) return;
  S.unvEl.hidden = !S.unverified;
  S.unvEl.textContent = S.unverified + (S.unverified === 1 ? " unverified task" : " unverified tasks");
}

function crewText(crew) { return (crew || []).map(rosterName).join(" · ") || "–"; }

function verdictOf(kit, t) {
  const [glyph, tone, label] = verdictParts(t);
  return kit.verdictCell(glyph, "var(--" + tone + ")", label);
}

function badge(parent, text, cls) { return el("span", "ledger-badge" + (cls ? " " + cls : ""), text, parent); }

function titleCell(t) {
  return (td) => {
    const p = taskCellParts(t);
    if (p.prefix) { el("span", "ledger-plan-pre", p.prefix, td); td.title = p.hint; }
    td.appendChild(document.createTextNode(p.text));
    if (t.src === "v1") badge(td, "v1");
    if (t.auto) badge(td, "auto");
    if (t.unverified_flag) badge(td, "unverified", "warn");
    const st = t.session_title;
    if (st && st !== p.text && st !== taskTitle(t)) el("span", "ledger-sub", st, td);
  };
}

function roundsCell(t) {
  const p = roundsParts(t.rounds);
  return (td) => {
    td.appendChild(document.createTextNode(p.main));
    if (p.sub) el("span", "ledger-sub", p.sub, td);
  };
}

function renderTasks(ctx, tasks) {
  const { kit } = ctx;
  const host = S.tasksEl;
  host.textContent = "";
  if (!tasks.length) {
    const e = el("div", "empty-card", null, host);
    el("b", null, "No tasks for this selection", e);
    e.appendChild(document.createTextNode("Tasks appear here as sessions declare a type of work."));
    return;
  }
  const wrapper = el("div", "tscroll ledger-tbox", null, host);
  wrapper.dataset.keepScroll = "1";
  const table = kit.table(wrapper,
    ["Task", "Area", "Type", "Crew", "Rework rounds", "Caught", "Usage", "Verdict"].map((h) => ({ t: h, tip: TASK_HEAD_TIPS[h], num: ["Rework rounds", "Caught", "Usage"].includes(h) })),
    tasks.map((t) => [titleCell(t), kit.areaCell(t.area, ctx.areaName(t.area), (x) => ctx.areas.some((a) => a.id === x)), t.type + (t.auto ? " · auto" : ""), crewText(t.crew),
      roundsCell(t),
      t.caught ? String(t.caught) : "–", compact(t.cost && t.cost.weighted), verdictOf(kit, t)]));
  table.querySelectorAll("tbody tr").forEach((tr, i) => {
    tr.classList.add("ledger-row");
    tr.tabIndex = 0;
    tr.addEventListener("click", () => openDrawer(ctx, tasks[i]));
    tr.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openDrawer(ctx, tasks[i]); });
  });
}

/* ---------- drawer ---------- */
function closeDrawer() {
  if (!S) return;
  S.drawer.hidden = true;
  S.drawerBody.textContent = "";
  S.openId = null;
}

async function openDrawer(ctx, t) {
  S.openId = t.id;
  S.drawer.hidden = false;
  const body = S.drawerBody;
  body.textContent = "";
  el("p", "note", "Loading the task record.", body);
  let rec = t, events = [];
  try {
    const d = await fetchLedger(ctx, "task", { id: t.id });
    rec = d.task || t;
    events = d.events || [];
  } catch (e) {
    if (!S || S.openId !== t.id) return;
    body.textContent = "";
    el("div", "err-line", "The full record could not be read (" + (e && e.message || e) + "). Showing the list entry.", body);
  }
  if (!S || S.openId !== t.id) return;
  paintDrawer(ctx, rec, events);
}

function dl(parent, rows) {
  const g = el("dl", "ledger-dl", null, parent);
  rows.forEach(([k, v]) => { if (v == null || v === "") return; el("dt", null, k, g); el("dd", null, v, g); });
}

function paintDrawer(ctx, t, events) {
  const { kit } = ctx;
  const body = S.drawerBody;
  body.keepTop = body.scrollTop;
  body.textContent = "";
  const head = el("div", "ledger-dhead", null, body);
  el("h2", null, taskTitle(t), head);
  const sub = el("div", "ledger-dsub", null, head);
  kit.areaCell(t.area, ctx.areaName(t.area), (x) => ctx.areas.some((a) => a.id === x))(el("span", null, null, sub));
  sub.appendChild(document.createTextNode(" " + t.type + " · " + statusWord(t.status)));
  if (t.session_title && t.session_title !== taskTitle(t)) el("div", "ledger-sub", t.session_title, head);
  if (t.src === "v1") badge(sub, "v1");
  if (t.auto) badge(sub, "auto");
  if (t.unverified_flag) badge(sub, "unverified", "warn");

  const r = t.rounds || {};
  dl(body, [
    ["Started", isoToHM(t.started)], ["Ended", isoToHM(t.ended)], ["Active time", t.duration_s ? duration(t.duration_s) : ""],
    ["Crew", crewText(t.crew)],
    ["Rework rounds", t.rounds ? [roundsParts(r).main, roundsParts(r).sub, r.left != null ? r.left + " left" : ""].filter(Boolean).join(" · ") : ""],
    ["Caught", String(t.caught || 0)], ["Edits", String(t.edits || 0)],
    ["Final verdict", t.final_verdict ? t.final_verdict.replace("_", " ") : "none"],
  ]);

  const vs = t.verdicts || [];
  el("h3", "ledger-dh3", "Verdicts", body);
  if (vs.length) {
    const ul = el("ul", "ledger-list", null, body);
    vs.forEach((v) => el("li", null, isoToHM(v.t) + "  " + rosterName(v.helper) + "  " + String(v.verdict).replace("_", " ") + "  (version " + v.version + ", " + v.findings + " findings)", ul));
  } else el("p", "note", "No verdict recorded.", body);

  const bh = (t.cost && t.cost.by_helper) || {};
  const rows = Object.entries(bh).sort((a, b) => b[1] - a[1]);
  el("h3", "ledger-dh3", "Cost by helper", body);
  if (rows.length) {
    const hb = el("div", "hbs", null, body);
    kit.hbars(hb, rows.map(([h, v]) => ({ label: rosterName(h), sub: TIER_NAMES[helperTier(h)], v, text: compact(v), color: "var(--m-" + helperTier(h) + ")" })), rows[0][1]);
    el("p", "note", "Total " + compact(t.cost.weighted) + " weighted", body).style.marginTop = "6px";
  } else el("p", "note", "No cost recorded.", body);

  const s = t.summary;
  el("h3", "ledger-dh3", "Summary", body);
  if (s) {
    dl(body, [["Goal", s.goal], ["Produced", s.produced], ["Decisions", s.decisions], ["Open", s.open], ["Review time", s.review_min ? s.review_min + " min" : ""]]);
  } else el("p", "note", "No summary was written for this task.", body);

  el("h3", "ledger-dh3", "Events", body);
  if (events.length) {
    const box = el("div", "ledger-events", null, body);
    box.dataset.keepScroll = "1";
    events.forEach((e) => {
      const line = el("div", "ledger-ev", null, box);
      el("span", "mono", isoToHM(e.t) || "", line);
      el("b", null, e.ev, line);
      const extra = [e.type, e.helper, e.verdict, e.label].filter(Boolean).join(" · ");
      if (extra) el("span", null, extra, line);
    });
  } else el("p", "note", "No events loaded.", body);
  body.scrollTop = body.keepTop || 0;
}

/* ---------- mount ---------- */
export default {
  id: "ledger",
  title: "Ledger",
  key: "l",
  mount(root, ctx) {
    root.textContent = "";
    const { ui } = ctx;
    S = { sample: sampleFlag(ctx), week: isoWeek(new Date()), area: "", type: "", status: "", statsSig: "", tasksSig: "", openId: null, unverified: 0, intOpen: false, bp: { open: new Set() } };
    const wrap = el("div", "screen-in ledger-in", null, root);
    const ph = el("div", "ph", null, wrap);
    el("div", "eyebrow", "Ledger", ph);
    el("h1", null, "Ledger", ph);
    S.lede = el("p", "lede", null, ph);
    el("p", "note", "Claude Code work only; calendar events are not recorded here.", ph);
    const bar = el("div", "ledger-bar", null, ph);
    const nav = el("div", "ledger-nav", null, bar);
    const prev = ui.button(nav, { label: "Previous week", onClick: () => { S.week = shiftWeek(S.week, -1); S.statsSig = S.tasksSig = S.reportSig = ""; refresh(); } });
    S.weekLabel = el("span", "mono ledger-week", null, nav);
    const next = ui.button(nav, { label: "Next week", onClick: () => { S.week = shiftWeek(S.week, 1); S.statsSig = S.tasksSig = S.reportSig = ""; refresh(); } });
    S.nextBtn = next;
    ui.button(nav, { label: "This week", onClick: () => { S.week = isoWeek(new Date()); S.statsSig = S.tasksSig = S.reportSig = ""; refresh(); } });
    S.unvEl = el("span", "chip ledger-unv", null, bar);
    S.unvEl.hidden = true;
    S.intEl = el("button", "chip ledger-int", null, bar);
    S.intEl.type = "button";
    S.intEl.hidden = true;
    S.intEl.addEventListener("click", () => { S.intOpen = !S.intOpen; paintIntegrity(); });
    if (S.sample) el("span", "chip", "Sample data", bar);
    void prev;
    S.intPanel = el("div", "card ledger-intpanel", null, wrap);
    S.intPanel.hidden = true;

    S.statsEl = el("div", "ledger-stats", null, wrap);
    const rc = card(wrap, "Weekly report", "The saved report for the selected week", "ledger-report");
    S.reportEl = el("div", "ledger-report-body", null, rc.c);
    S.reportSig = "";
    const tc = el("div", "card ledger-tasks", null, wrap);
    const ch = el("div", "ch", null, tc);
    const ct = el("div", "ct", null, ch);
    el("h3", null, "Tasks", ct);
    S.tasksNote = el("p", null, "Tasks whose span overlaps the week, newest first", ct);
    const filters = el("div", "ledger-filters", null, ch);
    S.areaDd = ui.dropdown(el("span", null, null, filters), { items: [{ value: "", label: "All areas" }].concat(ctx.areas.map((a) => ({ value: a.id, label: a.name }))), value: "", onChange: (v) => { S.area = v; S.tasksSig = ""; loadTasks(); } });
    S.typeDd = ui.dropdown(el("span", null, null, filters), { items: TYPES.map(([value, label]) => ({ value, label })), value: "", onChange: (v) => { S.type = v; S.tasksSig = ""; loadTasks(); } });
    S.statusDd = ui.dropdown(el("span", null, null, filters), { items: STATUSES.map(([value, label]) => ({ value, label })), value: "", onChange: (v) => { S.status = v; S.tasksSig = ""; loadTasks(); } });
    S.tasksEl = el("div", "ledger-tasklist", null, tc);
    S.tasksEl.style.marginTop = "12px";

    S.drawer = el("aside", "ledger-drawer", null, wrap);
    S.drawer.hidden = true;
    S.drawer.setAttribute("aria-label", "Task detail");
    const dh = el("div", "ledger-dbar", null, S.drawer);
    ui.button(dh, { label: "Close", onClick: closeDrawer });
    S.drawerBody = el("div", "ledger-dbody", null, S.drawer);
    S.drawerBody.dataset.keepScroll = "1";
    onKey = (ev) => { if (ev.key === "Escape" && S && !S.drawer.hidden) closeDrawer(); };
    document.addEventListener("keydown", onKey);

    function paintHead() {
      S.weekLabel.textContent = S.week;
      S.lede.textContent = "The record of every task. " + weekLabel(S.week) + ".";
      S.nextBtn.setDisabled(S.week >= isoWeek(new Date()), "This is the latest week");
    }

    async function loadStats() {
      try {
        const d = await fetchLedger(ctx, "stats", { week: S.week });
        if (!S) return;
        const sig = JSON.stringify(d);
        if (sig === S.statsSig) return;
        S.statsSig = sig;
        ctx.keepScroll(() => renderStats(ctx, d));
      } catch (e) {
        if (!S) return;
        S.statsSig = "";
        ctx.keepScroll(() => {
          S.statsEl.textContent = "";
          const em = el("div", "empty-card", null, S.statsEl);
          el("b", null, "No ledger data yet", em);
          em.appendChild(document.createTextNode("The ledger has not answered (" + (e && e.message || e) + "). Charts appear once sessions have been recorded."));
          S.unverified = 0;
          S.integrity = null;
          paintUnverified();
          paintIntegrity();
        });
      }
    }
    async function loadTasks() {
      try {
        const d = await fetchLedger(ctx, "tasks", { week: S.week, area: S.area, type: S.type, status: S.status, limit: 200 });
        if (!S) return;
        const sig = JSON.stringify(d);
        if (sig === S.tasksSig) return;
        S.tasksSig = sig;
        ctx.keepScroll(() => renderTasks(ctx, d.tasks || []));
      } catch (e) {
        if (!S) return;
        S.tasksSig = "";
        ctx.keepScroll(() => {
          S.tasksEl.textContent = "";
          const em = el("div", "empty-card", null, S.tasksEl);
          el("b", null, "No tasks to show", em);
          em.appendChild(document.createTextNode("The task list could not be read (" + (e && e.message || e) + ")."));
        });
      }
    }
    async function loadReport() {
      const week = S.week;
      let md = null, msg = "";
      try {
        if (S.sample) throw new Error("report: sample mode");
        const d = await ctx.api.get("/api/v2/ledger/report?week=" + encodeURIComponent(week));
        md = d && typeof d.markdown === "string" ? d.markdown : "";
      } catch (e) {
        const m = String(e && e.message || e);
        msg = (e && e.status === 404) || S.sample ? "No saved report for this week. Run /weekly-report to create one."
          : "The weekly report could not be read (" + m + ").";
      }
      if (!S || S.week !== week) return;
      const sig = week + "|" + (md == null ? "!" + msg : md);
      if (sig === S.reportSig) return;
      S.reportSig = sig;
      ctx.keepScroll(() => {
        S.reportEl.textContent = "";
        if (md == null) el("p", "note", msg, S.reportEl);
        else if (!md.trim()) el("p", "note", "The saved report is empty.", S.reportEl);
        else renderMarkdown(S.reportEl, md);
      });
    }
    async function refresh() {
      paintHead();
      await Promise.all([loadStats(), loadTasks(), loadReport()]);
    }
    ctx.every(15000, refresh);
  },
  unmount() {
    if (onKey) document.removeEventListener("keydown", onKey);
    onKey = null;
    S = null;
  },
};
