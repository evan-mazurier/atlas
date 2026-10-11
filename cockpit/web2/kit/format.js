/* Pure helpers: no DOM, so `node --test` can run them (cockpit/test_web2_format.mjs). */

export const MINUS = "−";

export function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function pad2(n) { return (n < 10 ? "0" : "") + n; }

/** Hours as a float (8.5) to "08:30". */
export function fmtT(hr) {
  let hh = Math.floor(hr + 1e-9);
  let mm = Math.round((hr - hh) * 60);
  if (mm === 60) { hh += 1; mm = 0; }
  return pad2(hh) + ":" + pad2(mm);
}

/** "HH:MM" to hours as a float; null when it does not parse. */
export function hm(s) {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(s || ""));
  return m ? Number(m[1]) + Number(m[2]) / 60 : null;
}

/** An ISO datetime to the local "HH:MM". */
export function isoToHM(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : pad2(d.getHours()) + ":" + pad2(d.getMinutes());
}

/** 1234 -> "1k", 2270000 -> "2.27M". */
export function tokens(n) {
  n = Number(n) || 0;
  return n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);
}

/** Millions with two decimals for chart ticks and tips: 620000 -> "0.62M". */
export function millions(v) { return (v / 1e6).toFixed(2) + "M"; }

/** Ticks [0, step, 2 step, ...] with a "nice" step so that the last tick is >= max. */
export function niceTicks(max, count = 3) {
  if (!(max > 0)) return [0, 1];
  const raw = max / count;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / pow;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * pow;
  const out = [0];
  while (out[out.length - 1] < max - 1e-9) out.push(Math.round((out[out.length - 1] + step) * 1e9) / 1e9);
  return out;
}

/** Decimals needed to print every multiple of a tick step distinctly: 1 -> 0, 2.5 -> 1, 0.5 -> 1, 0.05 -> 2, 0.025 -> 3 (max 3). */
export function tickDecimals(step) {
  if (!(step > 0)) return 0;
  for (let d = 0; d < 3; d++) {
    const x = step * Math.pow(10, d);
    if (Math.abs(x - Math.round(x)) < 1e-6) return d;
  }
  return 3;
}

/** ISO week "YYYY-Www" of a date (local time). */
export function isoWeek(d) {
  const t = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
  const day = t.getUTCDay() || 7;
  t.setUTCDate(t.getUTCDate() + 4 - day);
  const y = t.getUTCFullYear();
  const w = Math.ceil(((t - Date.UTC(y, 0, 1)) / 86400000 + 1) / 7);
  return y + "-W" + pad2(w);
}

/** Monday of an ISO week, as a Date at local midnight. */
export function weekStart(week) {
  const m = /^(\d{4})-W(\d{2})$/.exec(week);
  if (!m) return null;
  const jan4 = new Date(Number(m[1]), 0, 4);
  const mon = new Date(jan4);
  mon.setDate(jan4.getDate() - ((jan4.getDay() || 7) - 1) + (Number(m[2]) - 1) * 7);
  return mon;
}

export function shiftWeek(week, n) {
  const s = weekStart(week);
  if (!s) return week;
  s.setDate(s.getDate() + 7 * n);
  return isoWeek(s);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export function weekLabel(week) {
  const s = weekStart(week);
  return s ? "Week of " + s.getDate() + " " + MONTHS[s.getMonth()] : week;
}

/** Greedy lane assignment: items [{start, end}] (hours) -> lane index per item, no overlap in a lane. */
export function assignLanes(items) {
  const order = items.map((it, i) => i).sort((a, b) => items[a].start - items[b].start || items[a].end - items[b].end);
  const ends = [];
  const lane = new Array(items.length).fill(0);
  for (const i of order) {
    let k = ends.findIndex((e) => e <= items[i].start + 1e-9);
    if (k < 0) { k = ends.length; ends.push(0); }
    ends[k] = items[i].end;
    lane[i] = k;
  }
  return lane;
}

const HELPER_NAMES = {
  chief: "Chief", scout: "Scout", explore: "Scout", sweeper: "Clerk", researcher: "Analyst", implementer: "Builder",
  drafter: "Writer", "test-runner": "Lab Tech", reviewer: "Inspector", "senior-reviewer": "Senior Partner",
};
export function helperName(type) { return HELPER_NAMES[String(type || "").toLowerCase()] || String(type || ""); }

/** Direction text for a KPI: {text, tone} with tone ok | bad | neu; `good` is "up", "down" or "none". */
export function delta(value, prev, good, unit = "") {
  const v = Number(value), p = Number(prev);
  if (!isFinite(v) || !isFinite(p)) return { text: "", tone: "neu" };
  const d = v - p;
  if (d === 0) return { text: "no change", tone: "neu" };
  const mag = Math.abs(unit === "%rel" ? (p ? (d / p) * 100 : 0) : d);
  const num = unit === "%rel" ? Math.round(mag) + " %" : unit === "pts" ? Math.round(mag * 10) / 10 + " pts" : String(Math.round(mag * 10) / 10);
  const tone = good === "none" ? "neu" : (d > 0) === (good === "up") ? "ok" : "bad";
  return { text: (d > 0 ? "▲ " : "▼ ") + num, tone };
}

const HELPER_TIERS = { chief: "opus", "senior-reviewer": "opus", scout: "haiku", explore: "haiku", sweeper: "haiku", "test-runner": "haiku" };
export function helperTier(type) { return HELPER_TIERS[String(type || "").toLowerCase()] || "sonnet"; }

/** 6200000 -> "6.2M", 310000 -> "310k", 0 -> "0". */
export function compact(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return Math.round(n / 1e3) + "k";
  return String(Math.round(n));
}

/** "2026-09-26" -> "26 Sep". */
export function dayLabel(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  return m ? Number(m[3]) + " " + MONTHS[Number(m[2]) - 1] : String(iso || "");
}
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
/** "2026-09-26" -> "Sat 26 Sep". */
export function dayTitle(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  if (!m) return String(iso || "");
  return WEEKDAYS[new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3])).getDay()] + " " + dayLabel(iso);
}

/** Seconds as "1 h 20 min" / "45 min". */
export function duration(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? h + " h" + (m ? " " + m + " min" : "") : m + " min";
}
