// Ledger bypass rows (cockpit/web2/screens/ledger-bypass.js). Run: node --test cockpit/test_web2_bypass.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { bypassRowHTML, bypassListHTML, noteLabel } from "./web2/screens/ledger-bypass.js";

const BP = {
  key: "2026-10-10T10:00:00+02:00|s1", t: "2026-10-10T10:00:00+02:00", tool: "Bash", matched: "protected-folder", denied: true,
  report: { headline: "The orchestrator tried to read the ledger record of 9 Oct with a shell command. Blocked.", own_words: "look <b>x</b>",
    would_change: "The command looks read-only.", context: 'Session "A", type chore.', after: "No further activity recorded in the next 5 minutes.",
    reading: "Isolated read.", level: "info", notes: [], actor_is_main: false },
};
const NOTE = { t: "2026-10-10T11:05:00+02:00", by: "main", text: "checked <i>fine</i>" };

test("bypass row is collapsed by default, with a keyboard-reachable button and no command toggle", () => {
  const h = bypassRowHTML(BP, false);
  assert.match(h, /<button type="button" class="ledger-bp-h" aria-expanded="false"/);
  assert.match(h, /class="ledger-bp-body" id="[^"]+" hidden/);
  assert.match(h, /lv-info/);
  assert.match(h, /In its own words/);
  assert.match(h, /Would it have changed anything\?/);
  assert.ok(!/show command|ledger-bp-cmd/.test(h));
  assert.ok(!/ledger-bp-notes/.test(h));
});

test("bypass row open state and level colour class", () => {
  const h = bypassRowHTML(Object.assign({}, BP, { report: Object.assign({}, BP.report, { level: "alert" }) }), true);
  assert.match(h, /aria-expanded="true"/);
  assert.doesNotMatch(h, /class="ledger-bp-body"[^>]* hidden/);
  assert.match(h, /ledger-bp-reading lv-alert/);
});

test("notes sit below the facts, muted, labelled as unverified, escaped", () => {
  const h = bypassRowHTML(Object.assign({}, BP, { report: Object.assign({}, BP.report, { notes: [NOTE] }) }), true);
  assert.ok(h.indexOf("</dl>") < h.indexOf("ledger-bp-notes"));
  assert.match(h, /Note by the main session \(not verified\), 10 Oct 11:05/);
  assert.match(h, /checked &lt;i&gt;fine&lt;\/i&gt;/);
  assert.ok(!h.includes("<i>fine"));
});

test("note label names the agent involved when the actor is the main session", () => {
  assert.match(noteLabel(NOTE, true), /^Note by the agent involved \(not verified\), 10 Oct 11:05$/);
  assert.match(noteLabel(NOTE, false), /^Note by the main session \(not verified\)/);
});

test("all notes are kept in order", () => {
  const n2 = { t: "2026-10-10T12:00:00+02:00", by: "main", text: "later" };
  const h = bypassRowHTML(Object.assign({}, BP, { report: Object.assign({}, BP.report, { notes: [NOTE, n2] }) }), true);
  assert.ok(h.indexOf("11:05") < h.indexOf("12:00"));
});

test("bypass row escapes every dynamic value", () => {
  const h = bypassRowHTML(BP, true);
  assert.ok(!h.includes("<b>x</b>"));
  assert.match(h, /look &lt;b&gt;x&lt;\/b&gt;/);
  const evil = bypassRowHTML({ key: 'k"><img src=x>', t: "2026-10-10T10:00:00+02:00", tool: "<i>", matched: "<u>", denied: false }, false);
  assert.ok(!evil.includes("<img") && !evil.includes("<i>") && !evil.includes("<u>"));
});

test("bypass row without a report still renders", () => {
  const h = bypassRowHTML({ key: "k", t: "2026-10-09T09:00:00+02:00", tool: "Bash", matched: "m", denied: true }, false);
  assert.match(h, /No report is available/);
});

test("bypass list keeps open rows by key", () => {
  const other = Object.assign({}, BP, { key: "other|s2" });
  const h = bypassListHTML([BP, other], { open: new Set([BP.key]) });
  assert.equal((h.match(/aria-expanded="true"/g) || []).length, 1);
  assert.equal((h.match(/class="ledger-bp lv-/g) || []).length, 2);
});
