// Pure helpers of cockpit/web2/kit/format.js. Run: node --test cockpit/test_web2_format.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
  assignLanes, compact, delta, dayLabel, dayTitle, duration, esc, fmtT, helperName, helperTier, hm, isoWeek, niceTicks, tickDecimals, shiftWeek, tokens, weekStart,
} from "./web2/kit/format.js";

test("fmtT and hm round-trip", () => {
  assert.equal(fmtT(8.5), "08:30");
  assert.equal(fmtT(15.999999), "16:00");
  assert.equal(hm("08:30"), 8.5);
  assert.equal(hm("bad"), null);
});

test("tokens and compact", () => {
  assert.equal(tokens(2270000), "2.27M");
  assert.equal(tokens(84000), "84k");
  assert.equal(compact(6200000), "6.2M");
  assert.equal(compact(310000), "310k");
  assert.equal(compact(0), "0");
});

test("niceTicks always reach the maximum", () => {
  for (const max of [0.2, 0.77, 1.3, 4, 33, 1234]) {
    const t = niceTicks(max, 3);
    assert.equal(t[0], 0);
    assert.ok(t[t.length - 1] >= max, `max ${max} ticks ${t}`);
    assert.ok(t.length <= 6);
  }
  assert.deepEqual(niceTicks(0), [0, 1]);
});

test("tickDecimals keeps usage ticks distinct", () => {
  assert.deepEqual([1, 2, 5, 0.5, 0.2, 0.05, 0.025, 2.5].map(tickDecimals), [0, 0, 0, 1, 1, 2, 3, 1]);
  for (const max of [0.03, 0.07, 0.12, 0.3, 0.77, 2.4, 9]) {
    const t = niceTicks(max, 3);
    const d = tickDecimals(t[1]);
    const lab = t.map((v) => v.toFixed(d));
    assert.equal(new Set(lab).size, lab.length, `max ${max} labels ${lab}`);
  }
});

test("ISO weeks", () => {
  assert.equal(isoWeek(new Date(2026, 9, 9)), "2026-W41");
  assert.equal(isoWeek(new Date(2026, 0, 1)), "2026-W01");
  assert.equal(isoWeek(new Date(2024, 11, 30)), "2025-W01");
  assert.equal(shiftWeek("2026-W41", 1), "2026-W42");
  assert.equal(shiftWeek("2026-W01", -1), "2025-W52");
  assert.equal(weekStart("2026-W41").getDate(), 5);
});

test("lane assignment never overlaps inside a lane", () => {
  const items = [{ start: 9, end: 11 }, { start: 10, end: 12 }, { start: 11, end: 13 }, { start: 9.5, end: 10 }, { start: 13, end: 14 }];
  const lanes = assignLanes(items);
  for (let a = 0; a < items.length; a++) for (let b = a + 1; b < items.length; b++) {
    if (lanes[a] === lanes[b]) assert.ok(items[a].end <= items[b].start || items[b].end <= items[a].start, `${a} ${b}`);
  }
  assert.equal(Math.max(...lanes), 1);
  assert.deepEqual(assignLanes([]), []);
});

test("delta tones follow the good direction", () => {
  assert.deepEqual(delta(23, 18, "up"), { text: "▲ 5", tone: "ok" });
  assert.equal(delta(1, 0, "down").tone, "bad");
  assert.equal(delta(14, 11, "none").tone, "neu");
  assert.deepEqual(delta(6200000, 6750000, "down", "%rel"), { text: "▼ 8 %", tone: "ok" });
  assert.equal(delta(61, 57, "up", "pts").text, "▲ 4 pts");
  assert.equal(delta(5, 5, "up").text, "no change");
});

test("names, escaping, dates", () => {
  assert.equal(helperName("implementer"), "Builder");
  assert.equal(helperName("zzz"), "zzz");
  assert.equal(helperTier("chief"), "opus");
  assert.equal(helperTier("reviewer"), "sonnet");
  assert.equal(helperTier("scout"), "haiku");
  assert.equal(esc('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;");
  assert.equal(dayLabel("2026-09-26"), "26 Sep");
  assert.equal(dayTitle("2026-09-26"), "Sat 26 Sep");
  assert.equal(duration(5400), "1 h 30 min");
  assert.equal(duration(2700), "45 min");
});
