/* Ledger integrity badge text and details panel (docs/v2/contracts.md §5.2, §6.4 rule A, §8.5).
   Pure string building (no DOM) so node can test it; every dynamic value goes through esc(). The verdicts and signs are
   computed by code from the hash chain (ledger2/ledger_chain.py); the operator acknowledges a break by typing
   /ack-break in a session, never from this screen. */
import { dayLabel, esc, isoToHM } from "../kit/format.js";

const VERDICT_WORD = { accidental: "Looks accidental", tampering: "Looks like tampering", unclear: "Unclear" };

/** "Ledger integrity: intact" | "intact, 2 acknowledged" | "broken, 1 unacknowledged". */
export function integrityLabel(i) {
  if (!i) return "";
  const status = i.status || (i.intact ? "intact" : "broken");
  if (status !== "broken") return "Ledger integrity: " + status;
  const open = (i.breaks || []).filter((b) => !b.acknowledged).length;
  const am = ((i.anchor && i.anchor.mismatches) || []).length;
  const parts = [open ? open + " unacknowledged" : "", am ? am + " anchor mismatch" + (am > 1 ? "es" : "") : ""].filter(Boolean);
  return "Ledger integrity: broken" + (parts.length ? ", " + parts.join(", ") : "");
}

/** Chip colour class: bad = broken, warn = intact with acknowledged breaks, ok otherwise. */
export function integrityLevel(i) {
  if (!i) return "ok";
  if (i.intact === false || i.status === "broken") return "bad";
  const al = anchorLevel(i.anchor);
  if (al === "bad") return "bad";
  if ((i.breaks || []).length || al !== "ok") return "warn";
  return "ok";
}

function when(t) {
  if (!t) return "";
  return dayLabel(String(t).slice(0, 10)) + " " + (isoToHM(t) || "");
}

function signs(title, list, cls) {
  if (!list || !list.length) return "";
  return '<div class="ledger-brk-signs ' + cls + '"><span class="ledger-brk-signs-t">' + esc(title) + "</span><ul>"
    + list.map((s) => "<li>" + esc(s) + "</li>").join("") + "</ul></div>";
}

export function breakHTML(b) {
  const v = VERDICT_WORD[b.verdict] ? b.verdict : "unclear";
  const ack = b.acknowledged
    ? '<span class="ledger-brk-ack">Acknowledged by the operator' + (b.ack_t ? ", " + esc(when(b.ack_t)) : "") + "</span>"
    : '<span class="ledger-brk-ack ledger-brk-open">Not acknowledged. Typing <code>/ack-break ' + esc(b.id) + "</code> in a session acknowledges it.</span>";
  return '<li class="ledger-brk vd-' + v + '" data-id="' + esc(b.id) + '">'
    + '<div class="ledger-brk-h"><span class="ledger-brk-id">' + esc(b.id) + "</span>"
    + '<span class="ledger-brk-tag vd-' + v + '">' + esc(VERDICT_WORD[v]) + "</span></div>"
    + '<p class="ledger-brk-sum">' + esc(b.summary || "") + "</p>"
    + '<p class="ledger-brk-where">' + esc(b.file + ", line " + b.line) + "</p>"
    + signs("Signs of an accident (" + ((b.signs && b.signs.accidental) || []).length + " of " + ((b.signs && b.signs.of) || 0) + ")", b.signs && b.signs.accidental, "sg-acc")
    + signs("Signs of tampering", b.signs && b.signs.tampering, "sg-tam")
    + ack + "</li>";
}

/** One line about the outside anchor: not set up, all match, or the mismatch (red). */
export function anchorHTML(i, now) {
  const a = i && i.anchor;
  if (!a) return "";
  if (!a.configured) return '<p class="note ledger-anchor">Outside anchor: not set up.</p>';
  const bad = a.mismatches || [];
  const lvl = anchorLevel(a, now);
  let line = "";
  if (a.check_disabled) {
    line = "Outside anchor: check disabled.";
  } else if (a.remote === "unreachable") {
    line = "Outside anchor: remote unreachable since " + (a.unreachable_since ? when(a.unreachable_since) : "the last check") + ".";
  } else if (a.remote === "checking") {
    line = "Outside anchor: checking the remote.";
  } else if (!bad.length && (a.remote === "ok" || a.remote === undefined)) {
    const d = a.last_anchored_day ? dayLabel(a.last_anchored_day) : "none yet";
    line = "Outside anchor: last day anchored " + d + ", all match.";
  }
  if (line && a.remote === "ok" && a.last_remote_commit_at) {
    const days = anchorAgeDays(a, now);
    line += " Last remote update " + when(a.last_remote_commit_at) + (days >= 1 ? " (" + Math.floor(days) + " days ago)" : "") + ".";
  }
  if (line && a.last_push_ok_at) line += " Last successful push " + when(a.last_push_ok_at) + ".";
  const head = line ? '<p class="note ledger-anchor ' + lvl + '">' + esc(line) + "</p>" : "";
  if (!bad.length) return head;
  return head + '<ul class="ledger-brklist ledger-anchor-bad">' + bad.map((m) =>
    '<li class="ledger-brk vd-' + (VERDICT_WORD[m.verdict] ? m.verdict : "unclear") + '"><div class="ledger-brk-h"><span class="ledger-brk-id">Outside anchor'
    + (m.day ? ": " + esc(m.day) : "") + '</span><span class="ledger-brk-tag vd-' + (VERDICT_WORD[m.verdict] ? m.verdict : "unclear") + '">'
    + esc(VERDICT_WORD[m.verdict] || "Unclear") + "</span></div>"
    + '<p class="ledger-brk-sum">' + esc(m.summary || "") + "</p>"
    + (m.reason ? '<p class="ledger-brk-where">' + esc((m.lines ? "line " + m.lines + ", " : "") + m.reason) + "</p>" : "") + "</li>").join("") + "</ul>";
}

/** Days since the newest commit on the outside anchor remote (0 when unknown). */
export function anchorAgeDays(a, now) {
  const t = a && a.last_remote_commit_at ? Date.parse(a.last_remote_commit_at) : NaN;
  if (isNaN(t)) return 0;
  return Math.max(0, ((now == null ? Date.now() : now) - t) / 86400000);
}

/** "warn" (amber) when the remote is unreachable or the last remote update is older than 2 days, "bad" (red) when a
 *  mismatch exists or the last update is older than 7 days, else "ok". */
export function anchorLevel(a, now) {
  if (!a || !a.configured) return "ok";
  if ((a.mismatches || []).length) return "bad";
  const d = anchorAgeDays(a, now);
  if (a.remote === "ok" && d > 7) return "bad";
  if (a.check_disabled) return "warn";
  if (a.remote === "unreachable") {
    const since = a.unreachable_since ? Date.parse(a.unreachable_since) : NaN;
    const days = isNaN(since) ? 0 : Math.max(0, ((now == null ? Date.now() : now) - since) / 86400000);
    return a.expected && days > 2 ? "bad" : "warn";      // red once it was set up and the remote stayed away for over 2 days
  }
  if (a.remote === "ok" && d > 2) return "warn";
  return "ok";
}

export function integrityPanelHTML(i, now) {
  if (!i) return "";
  const list = i.breaks || [];
  const anchor = anchorHTML(i, now);
  if (!list.length) return '<p class="note">No breaks in the hash chain (' + esc(String(i.lines || 0)) + " lines in " + esc(String(i.files || 0)) + " day files).</p>" + anchor;
  return '<p class="note">Each break is a place where a line no longer matches the hash of the line before it. The verdict is computed from signs in the file; it is a reading, not a proof.</p>'
    + '<ul class="ledger-brklist">' + list.map(breakHTML).join("") + "</ul>" + anchor;
}
