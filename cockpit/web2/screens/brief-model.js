/* Pure parts of the Brief screen (docs/v2/contracts.md §8.6): text rules and markup strings, no DOM,
   so `node --test cockpit/test_web2_brief.mjs` can run them. Every text that comes from data (titles and notes
   come from chat and Plan) goes through esc(). Colours only through tokens (look.css, areas via ctx.areaColor). */
import { esc } from "../kit/format.js";

export const MAX_ROWS = 8;
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parts(iso) { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || "")); return m ? [+m[1], +m[2], +m[3]] : null; }
function dow(iso) { const p = parts(iso); return p ? new Date(Date.UTC(p[0], p[1] - 1, p[2])).getUTCDay() : 0; }

/** "Tuesday 17 November" */
export function dayTitle(iso) {
  const p = parts(iso);
  return p ? DAYS[dow(iso)] + " " + p[2] + " " + MONTHS[p[1] - 1] : "";
}
/** "17 Nov" */
export function dayMon(iso) { const p = parts(iso); return p ? p[2] + " " + MON[p[1] - 1] : ""; }
/** "Sat 28 Nov" */
export function dayShort(iso) { const p = parts(iso); return p ? DOW[dow(iso)] + " " + p[2] + " " + MON[p[1] - 1] : ""; }
/** "Week of 23 Nov"; a missing week is the "Later" group. */
export function weekLabel(week) { return week ? "Week of " + dayMon(week) : "Later"; }

const plural = (n, one, many) => n + " " + (n === 1 ? one : many);

/** The summary line (design notes, not in this copy). The events part is left out when the day has no event. */
export function summaryLine(c) {
  c = c || {};
  const out = [
    c.do_not_miss + " to watch",
    c.act + " to act on",
    plural(c.today, "block today", "blocks today"),
  ];
  if (c.events_today > 0) out.push(c.events_done + " of " + c.events_today + " events done");
  out.push(plural(c.deadlines_30, "deadline in 30 days", "deadlines in 30 days"));
  return out.join(" · ");
}

/** Tone class of the days-left text: overdue crit, within 7 days warn, else none. */
export function daysClass(item) {
  if (item.overdue || item.days_left < 0) return "crit";
  if (item.days_left <= 7) return "warn";
  return "";
}

/** Group the "Next 30 days" items by their Monday (`week`), in order of first appearance. */
export function weekGroups(items) {
  const groups = [];
  (items || []).forEach((it) => {
    let g = groups.find((x) => x.week === (it.week || ""));
    if (!g) { g = { week: it.week || "", label: weekLabel(it.week), items: [] }; groups.push(g); }
    g.items.push(it);
  });
  return groups;
}

/** At most MAX_ROWS rows, then "and N more" until expanded. */
export function limitRows(items, expanded, max = MAX_ROWS) {
  const list = items || [];
  if (expanded || list.length <= max) return { shown: list, more: 0 };
  return { shown: list.slice(0, max), more: list.length - max };
}

export const itemKey = (it) => it.id + "|" + (it.occurrence_key || it.day);
export const todayKey = (it) => it.type + "|" + it.id + "|" + (it.occurrence || "");

/** Where snooze counts from: the item's displayed day when it is today or later, else (overdue) today. ISO days compare as text. */
export function snoozeBase(itemDay, today) { return itemDay && itemDay >= today ? itemDay : today; }

/** The three snooze presets, counted from `from` (see snoozeBase). */
export function snoozeTargets(day, addDays) {
  return [["+1 day", addDays(day, 1)], ["+3 days", addDays(day, 3)], ["+1 week", addDays(day, 7)]];
}

export const OPEN_LABEL = { inbox: "Open Inbox", finance: "Open Finance", team: "Open Team" };

/* ---------- markup ---------- */
const sw = (color) => '<span class="sw" style="background:' + esc(color) + '"></span>';
const btn = (act, key, label, extra) =>
  '<button type="button" class="ui-btn brief-btn" data-act="' + act + '" data-key="' + esc(key) + '"' + (extra || "") + ">" + esc(label) + "</button>";

function errLine(v, key) {
  const e = v.errors && v.errors[key];
  return e ? '<div class="brief-err" role="alert">' + esc(e) + "</div>" : "";
}

/** One deadline or follow-up row (Do not miss and Next 30 days). v = {areaColor, snooze, busy, errors}. */
export function deadlineRowHTML(it, v, opts) {
  const key = itemKey(it);
  const cls = daysClass(it);
  const when = opts && opts.withDay ? dayShort(it.day) + " · " : "";
  const tags = [];
  if (it.kind === "follow-up") tags.push("follow-up");
  if (it.priority === "high") tags.push("high");
  if (it.recurring) tags.push("repeats");
  const meta = tags.length ? ' <span class="brief-m">· ' + esc(tags.join(" · ")) + "</span>" : "";
  const notes = it.notes ? '<div class="brief-m brief-notes">' + esc(it.notes) + "</div>" : "";
  const busy = v.busy === key ? " disabled" : "";
  const open = v.snooze === key;
  return '<div class="brief-row" data-key="' + esc(key) + '">' + sw(v.areaColor(it.area)) +
    '<div class="brief-t"><span class="brief-title">' + esc(it.title) + "</span>" + meta + notes + "</div>" +
    '<span class="brief-d' + (cls ? " " + cls : "") + '">' + esc(when + it.days_text) + "</span>" +
    '<div class="brief-act">' + btn("done", key, "Done", busy) + btn("snooze", key, "Snooze", ' aria-expanded="' + (open ? "true" : "false") + '"' + busy) +
    btn("plan", key, "Open in Plan") + "</div>" +
    (open ? '<div class="brief-snz" data-key="' + esc(key) + '"></div>' : "") +
    errLine(v, key) + "</div>";
}

/** One "To act on" line. The API sends counts and categories only. */
export function actRowHTML(a, v) {
  const tone = a.priority === "high" ? "var(--warn)" : "var(--ctx-2)";
  const label = OPEN_LABEL[a.open];
  return '<div class="brief-row brief-act-row">' + sw(tone) +
    '<div class="brief-t"><span class="brief-title">' + esc(a.text) + "</span></div>" +
    (label ? '<div class="brief-act"><button type="button" class="ui-btn brief-btn" data-act="open" data-to="' + esc(a.open) + '">' + esc(label) + "</button></div>" : "") +
    "</div>";
}

export function timeText(t) {
  return t.start && t.end ? t.start + "-" + t.end : (t.start || "All day");
}

/** One Today row. Only events carry the Done toggle (can_mark_done). */
export function todayRowHTML(t, v) {
  const key = todayKey(t);
  const done = !!t.done;
  const toggle = t.can_mark_done
    ? '<div class="brief-act">' + btn("toggle", key, done ? "Undo" : "Done", ' aria-pressed="' + (done ? "true" : "false") + '"' + (v.busy === key ? " disabled" : "")) + "</div>"
    : "";
  return '<div class="brief-row' + (done ? " done" : "") + '" data-key="' + esc(key) + '">' +
    '<span class="brief-tm">' + esc(timeText(t)) + "</span>" + sw(v.areaColor(t.area)) +
    '<div class="brief-t"><span class="brief-title">' + (done ? '<span class="brief-tick" aria-hidden="true">✓ </span>' : "") + esc(t.title) + "</span></div>" +
    toggle + errLine(v, key) + "</div>";
}

function card(title, sub, body, extra) {
  return '<section class="card brief-card"' + (extra || "") + '><div class="ch"><div class="ct"><h3>' + esc(title) + "</h3>" +
    (sub ? "<p>" + esc(sub) + "</p>" : "") + "</div></div>" + body + "</section>";
}
const empty = (text) => '<div class="brief-empty">' + esc(text) + "</div>";
const moreBtn = (block, n) => '<button type="button" class="plan-link brief-more" data-act="more" data-block="' + block + '">and ' + n + " more</button>";

/** The four cards. v = {areaColor, expanded:{dnm}, snooze, busy, errors}. */
export function screenHTML(d, v) {
  const dnm = limitRows(d.do_not_miss, v.expanded && v.expanded.dnm);
  const c1 = card("Do not miss", d.do_not_miss.length ? plural(d.do_not_miss.length, "item", "items") : "",
    d.do_not_miss.length ? dnm.shown.map((it) => deadlineRowHTML(it, v)).join("") + (dnm.more ? moreBtn("dnm", dnm.more) : "") : empty("Nothing due. Nothing to chase."));
  const hasInbox = (d.act || []).some((a) => a.source === "inbox");
  const c2 = card("To act on", (d.act || []).length ? plural(d.act.length, "line", "lines") : "",
    (d.act || []).length ? d.act.map((a) => actRowHTML(a, v)).join("") + (hasInbox ? '<div class="brief-note">Mail lines reflect the last background sync (every 15 minutes).</div>' : "") : empty("Nothing to act on."));
  const c3 = card("Today", d.today.length ? plural(d.today.length, "block", "blocks") : "",
    d.today.length ? d.today.map((t) => todayRowHTML(t, v)).join("") : empty("No blocks today."));
  const groups = weekGroups(d.next);
  const c4 = card("Next 30 days", d.next.length ? plural(d.next.length, "deadline", "deadlines") : "",
    d.next.length ? groups.map((g) => '<div class="brief-wk">' + esc(g.label) + "</div>" + g.items.map((it) => deadlineRowHTML(it, v, { withDay: true })).join("")).join("") : empty("No deadlines in the next 30 days."));
  return c1 + c2 + c3 + c4;
}
