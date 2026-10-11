/* Bypass attempts card rows (docs/v2/contracts.md §5.2, §8.5): a themed collapsible row per attempt with a
   plain-language report. Pure string building (no DOM) so node can test it; every dynamic value goes through esc().
   The facts are computed by code from the ledger; notes are an account, shown muted below the facts. */
import { dayLabel, esc, isoToHM } from "../kit/format.js";

const LEVEL_WORD = { alert: "Alert", warn: "Warning", info: "Info" };

function line(label, text) {
  if (text == null || text === "") return "";
  return '<dt>' + esc(label) + '</dt><dd>' + esc(text) + '</dd>';
}

/** "Note by the main session (not verified), 10 Oct 11:05"; when the actor was the main session itself, the note
    is by "the agent involved". */
export function noteLabel(n, actorIsMain) {
  const when = dayLabel(String(n.t).slice(0, 10)) + " " + (isoToHM(n.t) || "");
  return "Note by " + (actorIsMain ? "the agent involved" : "the main session") + " (not verified), " + when;
}

function notesHTML(rep) {
  const notes = rep.notes || [];
  if (!notes.length) return "";
  return '<div class="ledger-bp-notes">' + notes.map((n) =>
    '<div class="ledger-bp-note"><span class="ledger-bp-notelbl">' + esc(noteLabel(n, rep.actor_is_main)) + '</span>'
    + '<span class="ledger-bp-notetxt">' + esc(n.text) + '</span></div>').join("") + '</div>';
}

/** One <li>. `open` is the expand state kept by the caller across repaints. */
export function bypassRowHTML(r, open) {
  const rep = r.report || null;
  const key = String(r.key || r.t || "");
  const level = rep && LEVEL_WORD[rep.level] ? rep.level : "info";
  const head = dayLabel(String(r.t).slice(0, 10)) + " " + (isoToHM(r.t) || "") + "  " + r.tool + "  " + (r.matched || "") + (r.denied ? "  refused" : "  allowed");
  const id = "bp-" + key.replace(/[^A-Za-z0-9_-]/g, "_");
  let body = "";
  if (rep) {
    body = '<p class="ledger-bp-headline"><b>' + esc(rep.headline) + '</b></p><dl class="ledger-bp-dl">'
      + line("In its own words", rep.own_words)
      + line("Would it have changed anything?", rep.would_change)
      + line("Context", rep.context)
      + line("Afterwards", rep.after)
      + '<dt>Reading</dt><dd class="ledger-bp-reading lv-' + level + '">' + esc(rep.reading) + '</dd>'
      + '</dl>' + notesHTML(rep);
  } else {
    body = '<p class="note">No report is available for this attempt.</p>';
  }
  return '<li class="ledger-bp lv-' + level + '" data-key="' + esc(key) + '">'
    + '<button type="button" class="ledger-bp-h" aria-expanded="' + (open ? "true" : "false") + '" aria-controls="' + esc(id) + '">'
    + '<span class="ledger-bp-chev" aria-hidden="true"></span><span class="ledger-bp-line">' + esc(head) + '</span>'
    + '<span class="ledger-bp-tag lv-' + level + '">' + esc(LEVEL_WORD[level]) + '</span></button>'
    + '<div class="ledger-bp-body" id="' + esc(id) + '"' + (open ? "" : " hidden") + '>' + body + '</div></li>';
}

/** Click handling for the whole list (event delegation). `st` = { open:Set }. Updates the DOM in place. */
export function wireBypassList(ul, st) {
  ul.addEventListener("click", (ev) => {
    const t = ev.target && ev.target.closest ? ev.target : null;
    if (!t) return;
    const li = t.closest("li.ledger-bp");
    const h = li && t.closest(".ledger-bp-h");
    if (!h) return;
    const key = li.dataset.key;
    const body = li.querySelector(".ledger-bp-body");
    const on = body.hidden;
    body.hidden = !on;
    h.setAttribute("aria-expanded", on ? "true" : "false");
    if (on) st.open.add(key); else st.open.delete(key);
  });
}

export function bypassListHTML(rows, st) {
  return (rows || []).map((r) => bypassRowHTML(r, st.open.has(String(r.key || r.t || "")))).join("");
}
