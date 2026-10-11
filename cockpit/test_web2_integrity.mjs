// Ledger integrity badge and panel (cockpit/web2/screens/ledger-integrity.js). Run: node --test cockpit/test_web2_integrity.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { integrityLabel, integrityLevel, integrityPanelHTML, breakHTML, anchorHTML, anchorLevel } from "./web2/screens/ledger-integrity.js";

const B1 = { file: "events-2026-10-09.jsonl", line: 11, id: "2026-10-09:11:ab12cd34", verdict: "tampering",
  summary: "Looks like tampering: <b>x</b>.", signs: { accidental: [], tampering: ["its link matches no line"], of: 6 }, acknowledged: false, ack_t: null };
const B2 = { file: "events-2026-10-09.jsonl", line: 30, id: "2026-10-09:30:00ff00ff", verdict: "accidental",
  summary: "Looks accidental: 2 of 6 signs: a; b.", signs: { accidental: ["a", "b"], tampering: [], of: 6 }, acknowledged: true, ack_t: "2026-10-10T09:05:00+02:00" };

test("badge text for intact, acknowledged and broken", () => {
  assert.equal(integrityLabel({ intact: true, status: "intact", breaks: [] }), "Ledger integrity: intact");
  assert.equal(integrityLabel({ intact: true, status: "intact, 1 acknowledged", breaks: [B2] }), "Ledger integrity: intact, 1 acknowledged");
  assert.equal(integrityLabel({ intact: false, status: "broken", breaks: [B1, B2] }), "Ledger integrity: broken, 1 unacknowledged");
  assert.equal(integrityLabel(null), "");
});

test("badge level: ok, warn with acknowledged breaks, bad when broken", () => {
  assert.equal(integrityLevel({ intact: true, status: "intact", breaks: [] }), "ok");
  assert.equal(integrityLevel({ intact: true, status: "intact, 1 acknowledged", breaks: [B2] }), "warn");
  assert.equal(integrityLevel({ intact: false, status: "broken", breaks: [B1] }), "bad");
});

test("break block shows verdict, signs and acknowledgement, and escapes everything", () => {
  const h = breakHTML(B1);
  assert.match(h, /vd-tampering/);
  assert.match(h, /Looks like tampering: &lt;b&gt;x&lt;\/b&gt;\./);
  assert.ok(!h.includes("<b>x"));
  assert.match(h, /Not acknowledged/);
  assert.match(h, /\/ack-break 2026-10-09:11:ab12cd34/);
  const a = breakHTML(B2);
  assert.match(a, /Acknowledged by the operator, 10 Oct 09:05/);
  assert.match(a, /Signs of an accident \(2 of 6\)/);
  assert.ok(!/em dash|—/.test(h + a));
});

test("outside anchor line: not set up, all match, or the mismatch in the badge and panel", () => {
  assert.match(anchorHTML({ anchor: { configured: false, mismatches: [] } }), /Outside anchor: not set up\./);
  assert.match(anchorHTML({ anchor: { configured: true, last_anchored_day: "2026-10-09", mismatches: [] } }), /Outside anchor: last day anchored .*9 Oct.*, all match\./);
  const m = { day: "2026-10-08", lines: 4, verdict: "tampering", reason: "the anchored line was changed", summary: "Looks like tampering: the record of 2026-10-08 no longer matches its outside anchor of 2026-10-09 <b>." };
  const bad = { intact: false, status: "broken", breaks: [], anchor: { configured: true, last_anchored_day: "2026-10-09", mismatches: [m] } };
  const h = integrityPanelHTML(bad);
  assert.match(h, /vd-tampering/);
  assert.match(h, /&lt;b&gt;/);
  assert.ok(!h.includes("<b>"));
  assert.ok(!/—/.test(h));
  assert.equal(integrityLabel(bad), "Ledger integrity: broken, 1 anchor mismatch");
  assert.equal(anchorHTML({}), "");
});

test("outside anchor: unreachable is amber, stale is amber after 2 days and red after 7, missing is a red card", () => {
  const NOW = Date.parse("2026-10-20T12:00:00+02:00");
  const base = { configured: true, last_anchored_day: "2026-10-19", mismatches: [], remote: "ok" };
  const un = { intact: true, status: "intact", breaks: [], anchor: { ...base, remote: "unreachable", unreachable_since: "2026-10-19T08:00:00+02:00" } };
  assert.match(anchorHTML(un, NOW), /ledger-anchor warn.*Outside anchor: remote unreachable since .*9 Oct/);
  assert.equal(integrityLevel(un), "warn");
  const longAgo = new Date(Date.now() - 3 * 86400e3).toISOString();
  assert.equal(integrityLevel({ intact: true, status: "intact", breaks: [], anchor: { ...base, remote: "unreachable", expected: true, unreachable_since: longAgo } }), "bad");
  assert.equal(anchorLevel({ ...base, remote: "unreachable", expected: true, unreachable_since: "2026-10-19T08:00:00+02:00" }, NOW), "warn");
  assert.equal(anchorLevel({ ...base, remote: "unreachable", expected: true, unreachable_since: "2026-10-16T08:00:00+02:00" }, NOW), "bad");
  assert.equal(anchorLevel({ ...base, remote: "unreachable", expected: false, unreachable_since: "2026-10-01T08:00:00+02:00" }, NOW), "warn");
  const off = { intact: true, status: "intact", breaks: [], anchor: { ...base, remote: "off", check_disabled: true } };
  assert.match(anchorHTML(off, NOW), /ledger-anchor warn.*Outside anchor: check disabled\./);
  assert.ok(!/all match/.test(anchorHTML(off, NOW) + anchorHTML(un, NOW)));
  const mk = (t) => ({ intact: true, status: "intact", breaks: [], anchor: { ...base, last_remote_commit_at: t } });
  assert.equal(anchorLevel(mk("2026-10-20T08:00:00+02:00").anchor, NOW), "ok");
  assert.equal(anchorLevel(mk("2026-10-17T08:00:00+02:00").anchor, NOW), "warn");
  assert.equal(anchorLevel(mk("2026-10-10T08:00:00+02:00").anchor, NOW), "bad");
  assert.match(anchorHTML(mk("2026-10-10T08:00:00+02:00"), NOW), /ledger-anchor bad.*\(10 days ago\)/);
  const miss = { intact: false, status: "broken", breaks: [], anchor: { ...base, missing: true,
    mismatches: [{ day: null, lines: null, reason: "", verdict: "unclear", summary: "Outside anchor missing: <x>." }] } };
  const h = integrityPanelHTML(miss, NOW);
  assert.match(h, /Outside anchor missing: &lt;x&gt;\./);
  assert.ok(!h.includes("<x>"));
  assert.equal(anchorLevel(miss.anchor, NOW), "bad");
});

test("panel lists every break, or says none", () => {
  const h = integrityPanelHTML({ intact: false, status: "broken", breaks: [B1, B2] });
  assert.equal((h.match(/class="ledger-brk /g) || []).length, 2);
  assert.match(integrityPanelHTML({ intact: true, status: "intact", breaks: [], lines: 5, files: 1 }), /No breaks/);
});
