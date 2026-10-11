// Pure helpers of cockpit/web2/screens/plan-core.js. Run: node --test cockpit/test_web2_plan.mjs
import test from "node:test";
import assert from "node:assert/strict";
import * as C from "./web2/screens/plan-core.js";

test("toMin and fromMin round-trip", () => {
  assert.equal(C.toMin("08:30"), 510);
  assert.equal(C.toMin("bad"), null);
  assert.equal(C.toMin(null), null);
  assert.equal(C.fromMin(510), "08:30");
  assert.equal(C.fromMin(1320), "22:00");
});

test("snap rounds to 15 minutes", () => {
  assert.equal(C.snap(7), 0);
  assert.equal(C.snap(8), 15);
  assert.equal(C.snap(607), 600);
  assert.equal(C.snap(623), 630);
});

test("the day is 07:00 to 22:00", () => {
  assert.equal(C.AXIS_MIN, 420);
  assert.equal(C.AXIS_MAX, 1320);
  assert.equal(C.EDIT_MIN, 420);
  assert.equal(C.EDIT_MAX, 1320);
});

test("moveRange keeps the length, snaps, and stays inside 07:00-22:00", () => {
  assert.deepEqual(C.moveRange(600, 660, 23), { start: 630, end: 690 });
  assert.deepEqual(C.moveRange(600, 660, -1000), { start: 420, end: 480 });
  assert.deepEqual(C.moveRange(600, 720, 5000), { start: 1200, end: 1320 });
});

test("resizeRange moves one edge and keeps a minimum length", () => {
  assert.deepEqual(C.resizeRange(600, 660, "end", 30), { start: 600, end: 690 });
  assert.deepEqual(C.resizeRange(600, 660, "end", -500), { start: 600, end: 615 });
  assert.deepEqual(C.resizeRange(600, 660, "start", -45), { start: 555, end: 660 });
  assert.deepEqual(C.resizeRange(600, 660, "start", 500), { start: 645, end: 660 });
  assert.deepEqual(C.resizeRange(600, 660, "end", 5000), { start: 600, end: 1320 });
});

test("createRange: a click gives the default hour, a drag gives the dragged range", () => {
  assert.deepEqual(C.createRange(603, 604), { start: 600, end: 660 });
  assert.deepEqual(C.createRange(600, 690), { start: 600, end: 690 });
  assert.deepEqual(C.createRange(690, 600), { start: 600, end: 690 });
  const late = C.createRange(1375, 1375);
  assert.ok(late.end <= 1320 && late.end - late.start >= 15);
  const early = C.createRange(100, 100);
  assert.equal(early.start, 420);
});

test("time to fraction and back", () => {
  assert.equal(C.minToFrac(420), 0);
  assert.equal(C.minToFrac(1320), 1);
  assert.equal(C.minToFrac(870), 0.5);
  assert.equal(C.minToFrac(0), 0);
  assert.equal(C.fracToMin(0.5), 870);
  assert.equal(C.fracToMin(2), 1320);
});

test("pointToSlot: day view runs time along x, week view along y with 7 columns", () => {
  const day = { orient: "h", left: 100, top: 0, width: 900, height: 50, cols: 1 };
  assert.deepEqual(C.pointToSlot(day, 100, 10), { col: 0, min: 420 });
  assert.deepEqual(C.pointToSlot(day, 550, 10), { col: 0, min: 870 });
  const wk = { orient: "v", left: 0, top: 100, width: 700, height: 480, cols: 7 };
  assert.deepEqual(C.pointToSlot(wk, 5, 100), { col: 0, min: 420 });
  assert.deepEqual(C.pointToSlot(wk, 699, 580), { col: 6, min: 1320 });
  assert.equal(C.pointToSlot(wk, 350, 340).col, 3);
  assert.equal(C.pointToSlot(wk, 350, 340).min, 870);
  assert.equal(C.pointToSlot(wk, -50, 340).col, 0);
  assert.ok(C.inside(wk, 10, 200));
  assert.ok(!C.inside(wk, 10, 50));
});

test("packRows puts overlapping blocks in separate rows", () => {
  const p = C.packRows([{ start: 600, end: 660 }, { start: 630, end: 700 }, { start: 660, end: 720 }, { start: 800, end: 830 }]);
  assert.equal(p.rows, 2);
  assert.notEqual(p.row[0], p.row[1]);
  assert.equal(p.row[2], p.row[0]);
  assert.equal(p.row[3], 0);
  assert.deepEqual(C.packRows([]), { row: [], rows: 0 });
});

test("dates: week days start on Monday, labels, ranges", () => {
  assert.equal(C.mondayOf("2026-10-09"), "2026-10-05");
  assert.equal(C.mondayOf("2026-10-11"), "2026-10-05");
  assert.equal(C.mondayOf("2026-10-12"), "2026-10-12");
  assert.deepEqual(C.weekDays("2026-10-09"), ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]);
  assert.equal(C.addDays("2026-10-31", 1), "2026-11-01");
  assert.equal(C.addDays("2026-03-01", -1), "2026-02-28");
  assert.equal(C.dayDiff("2026-10-05", "2026-10-09"), 4);
  assert.equal(C.dayShort("2026-10-09"), "Fri 9 Oct");
  assert.equal(C.rangeLabel("day", "2026-10-09"), "Fri 9 Oct 2026");
  assert.equal(C.rangeLabel("week", "2026-10-09"), "5 to 11 Oct 2026");
  assert.equal(C.rangeLabel("week", "2026-10-29"), "26 Oct to 1 Nov 2026");
  assert.deepEqual(C.fetchRange("2026-10-09"), { from: "2026-10-05", to: "2026-10-11" });
  assert.equal(C.dayFromNum(C.dayNum("2026-10-09")), "2026-10-09");
  assert.equal(C.dayNum("2026-10-10") - C.dayNum("2026-10-09"), 1);
});

test("version gate ignores anything older than what was seen", () => {
  const g = C.makeVersionGate();
  assert.equal(g.accept(5), true);
  assert.equal(g.accept(4), false);
  assert.equal(g.seen(), 5);
  assert.equal(g.accept(5), true);
  assert.equal(g.accept(9), true);
  assert.equal(g.accept(7), false);
  assert.equal(g.seen(), 9);
  g.note(12);
  assert.equal(g.seen(), 12);
  g.note(3);
  assert.equal(g.seen(), 12);
  assert.equal(g.accept(undefined), true);
  assert.equal(g.seen(), 12);
});

const EVS = () => [
  { id: "a", occurrence: "2026-10-10", day: "2026-10-10", title: "One", start: "10:00", end: "11:00", recur: null },
  { id: "r", occurrence: "2026-10-09", day: "2026-10-09", title: "Series", start: "06:00", end: "08:00", recur: { freq: "daily" } },
  { id: "r", occurrence: "2026-10-10", day: "2026-10-10", title: "Series", start: "06:00", end: "08:00", recur: { freq: "daily" } },
];

test("moveEvent: a plain event moves; a same-day series occurrence only retimes that date", () => {
  const m = C.moveEvent(EVS(), "a", "2026-10-10", { day: "2026-10-11", start: "12:00", end: "13:00" });
  const a = m.find((e) => e.id === "a");
  assert.equal(a.day, "2026-10-11");
  assert.equal(a.occurrence, "2026-10-11");
  assert.equal(a.start, "12:00");
  const s = C.moveEvent(EVS(), "r", "2026-10-10", { day: "2026-10-10", start: "06:30", end: "08:30" });
  assert.equal(s.filter((e) => e.id === "r")[0].start, "06:00");
  assert.equal(s.filter((e) => e.id === "r")[1].start, "06:30");
});

test("moveEvent: a series occurrence moved to another day becomes a one-off, the date leaves the series", () => {
  const m = C.moveEvent(EVS(), "r", "2026-10-10", { day: "2026-10-12", start: "09:00", end: "10:00" }, "evt_tmp1");
  assert.equal(m.filter((e) => e.id === "r").length, 1);
  const n = m.find((e) => e.id === "evt_tmp1");
  assert.equal(n.day, "2026-10-12");
  assert.equal(n.recur, null);
  assert.equal(n.from_series, "r");
  assert.equal(n.title, "Series");
});

test("restoreEvents puts back exactly what an edit replaced", () => {
  const before = EVS();
  const saved = C.entriesOf(before, "r");
  const moved = C.moveEvent(before, "r", "2026-10-10", { day: "2026-10-12", start: "09:00", end: "10:00" }, "evt_tmp1");
  const back = C.restoreEvents(moved.filter((e) => e.id !== "evt_tmp1"), "r", saved);
  assert.deepEqual(back.filter((e) => e.id === "r"), saved);
  assert.equal(back.filter((e) => e.id !== "r").length, 1);
});

test("patchSeries changes every occurrence; removeEvent only one date; null removes a field", () => {
  const p = C.patchSeries(EVS(), "r", { title: "Renamed", area: "md" });
  assert.deepEqual(p.filter((e) => e.id === "r").map((e) => e.title), ["Renamed", "Renamed"]);
  assert.equal(p.find((e) => e.id === "a").title, "One");
  const q = C.patchSeries(EVS(), "a", { notes: null, start: null, end: null });
  assert.equal(q.find((e) => e.id === "a").start, undefined);
  assert.equal(q.find((e) => e.id === "a").end, undefined);
  assert.equal(C.removeEvent(EVS(), "r", "2026-10-10").filter((e) => e.id === "r").length, 1);
  assert.equal(C.removeSeries(EVS(), "r").length, 1);
});

test("addEvent sets the occurrence and defaults; renameId swaps a temporary id", () => {
  const e = C.addEvent([], { id: "evt_tmp2", title: "N", day: "2026-10-11", start: "09:00", end: "10:00", area: "md" });
  assert.equal(e[0].occurrence, "2026-10-11");
  assert.equal(e[0].area_source, "explicit");
  assert.equal(C.renameId(e, "evt_tmp2", "evt_real")[0].id, "evt_real");
});

test("tasks: grouping by state, state change, add, remove, rename", () => {
  const list = [{ id: "1", state: "todo" }, { id: "2", state: "done" }, { id: "3", state: "weird" }, { id: "4", state: "doing" }];
  const g = C.groupTasks(list);
  assert.deepEqual(g.todo.map((t) => t.id), ["1", "3"]);
  assert.deepEqual(g.done.map((t) => t.id), ["2"]);
  assert.deepEqual(Object.keys(g), C.STATES);
  const tasks = { md: [{ id: "1", state: "todo", area: "md" }], job: [] };
  assert.equal(C.setTaskState(tasks, "1", "next").md[0].state, "next");
  assert.equal(tasks.md[0].state, "todo");
  assert.equal(C.addTask(tasks, { id: "9", area: "job" }).job.length, 1);
  assert.equal(C.removeTask(tasks, "1").md.length, 0);
  assert.equal(C.renameTask(tasks, "1", "x").md[0].id, "x");
  assert.equal(C.isReadOnlyTask({ id: "proj/t1", source: "plan.yaml" }), true);
  assert.equal(C.isReadOnlyTask({ id: "t_ab12", source: "tasks.yaml" }), false);
  assert.equal(C.fmtEstimate(30), "30 min");
  assert.equal(C.fmtEstimate(60), "1 h");
  assert.equal(C.fmtEstimate(150), "2 h 30");
});

test("review tray: length rounds up to the grid; scheduled marks and removal", () => {
  assert.equal(C.reviewLen({ review_min: 25 }), 30);
  assert.equal(C.reviewLen({ review_min: 15 }), 15);
  assert.equal(C.reviewLen({}), 30);
  const tray = [{ id: "rv_1", scheduled: null }, { id: "rv_2", scheduled: null }];
  const m = C.markScheduled(tray, "rv_1", { day: "2026-10-09", start: "10:00", end: "10:30" });
  assert.equal(m[0].scheduled.start, "10:00");
  assert.equal(tray[0].scheduled, null);
  assert.deepEqual(C.removeTray(tray, "rv_1").map((t) => t.id), ["rv_2"]);
});

test("mineBlocks picks one day; all-day events are separate", () => {
  const data = {
    events: [
      { id: "a", day: "2026-10-09", start: "10:00", end: "11:00", title: "A", area: "md" },
      { id: "b", day: "2026-10-09", title: "All day" },
      { id: "c", day: "2026-10-09", start: "12:00", end: "12:30", title: "Review: X", review_of: "t1" },
      { id: "d", day: "2026-10-10", start: "10:00", end: "11:00", title: "D" },
    ],
    placements: [{ session: "s", project: "p", task: "t", day: "2026-10-09", start: "14:00", end: "15:00", area: "train" }],
  };
  const m = C.mineBlocks(data, "2026-10-09");
  assert.deepEqual(m.map((b) => b.kind), ["mine", "review", "ro"]);
  assert.equal(m[0].start, 600);
  assert.equal(C.allDayEvents(data, "2026-10-09").length, 1);
});

test("recurText", () => {
  assert.equal(C.recurText(null), "");
  assert.equal(C.recurText({ freq: "daily", interval: 1, until: "2026-10-31" }), "daily until 2026-10-31");
  assert.equal(C.recurText({ freq: "weekly", interval: 2 }), "every 2 weeks");
});

test("buildRecur keeps keys it does not know and only applies what changed", () => {
  const prev = { freq: "weekly", interval: 2, count: 5, byday: "MO" };
  assert.deepEqual(C.buildRecur(prev, "daily", null, 1), { freq: "daily", interval: 2, count: 5, byday: "MO" });
  assert.deepEqual(C.buildRecur(prev, "weekly", "2026-12-01", 1), { freq: "weekly", interval: 2, byday: "MO", until: "2026-12-01" });
  assert.equal(C.buildRecur(prev, "none", null, 1), null);
  assert.deepEqual(C.buildRecur(null, "daily", null, 1), { freq: "daily", interval: 1 });
  assert.equal(prev.count, 5);
});

test("moveEvent: an all-day series occurrence can move to another day", () => {
  const evs = [{ id: "t", occurrence: "2026-10-09", day: "2026-10-09", title: "Trip", recur: { freq: "daily" } },
    { id: "t", occurrence: "2026-10-10", day: "2026-10-10", title: "Trip", recur: { freq: "daily" } }];
  const m = C.moveEvent(evs, "t", "2026-10-10", { day: "2026-10-14", start: null, end: null }, "evt_tmp9");
  const n = m.find((e) => e.id === "evt_tmp9");
  assert.equal(n.day, "2026-10-14");
  assert.equal(n.start, undefined);
  assert.equal(m.filter((e) => e.id === "t").length, 1);
});

test("patchSeries with no start key leaves the series times alone", () => {
  const evs = [{ id: "r", day: "2026-10-09", title: "S", start: "06:00", end: "08:00" }];
  const p = C.patchSeries(evs, "r", { title: "S2", area: null });
  assert.equal(p[0].start, "06:00");
  assert.equal(p[0].end, "08:00");
});

/* ---------- tasks: grouping by state, placing ---------- */
test("tasksByState: five columns in state order, hidden areas left out, area kept", () => {
  const tasks = { md: [{ id: "1", state: "todo" }, { id: "2", state: "doing" }], job: [{ id: "3", state: "todo" }, { id: "4", state: "review" }], train: [{ id: "5", state: "done" }] };
  const areas = [{ id: "md" }, { id: "job" }, { id: "train" }];
  const g = C.tasksByState(tasks, areas, {});
  assert.deepEqual(Object.keys(g), ["todo", "next", "doing", "review", "done"]);
  assert.deepEqual(g.todo.map((t) => t.id + t.area), ["1md", "3job"]);
  assert.deepEqual(g.review.map((t) => t.id), ["4"]);
  const h = C.tasksByState(tasks, areas, { job: true });
  assert.deepEqual(h.todo.map((t) => t.id), ["1"]);
  assert.equal(h.review.length, 0);
  assert.equal(C.STATE_LABELS.review, "Pending review");
});

test("taskLen: the estimate on the grid, 60 when unknown, never longer than the day", () => {
  assert.equal(C.taskLen({ estimate_min: 90 }), 90);
  assert.equal(C.taskLen({ estimate_min: 50 }), 60);
  assert.equal(C.taskLen({ estimate_min: null }), 60);
  assert.equal(C.taskLen({}), 60);
  assert.equal(C.taskLen({ estimate_min: 5000 }), 900);
});

test("plannedLabel and setPlanned", () => {
  assert.equal(C.plannedLabel({ day: "2026-10-12", start: "10:00", end: "11:30" }), "planned Mon 10:00");
  assert.equal(C.plannedLabel(null), "");
  assert.equal(C.plannedLabel({ day: "bad", start: "10:00" }), "");
  const tasks = { md: [{ id: "1", state: "todo" }], job: [{ id: "2" }] };
  const p = C.setPlanned(tasks, "1", { day: "2026-10-12", start: "10:00", end: "11:00", on: "me" });
  assert.equal(p.md[0].planned.start, "10:00");
  assert.equal(p.md[0].state, "todo");
  assert.equal(tasks.md[0].planned, undefined);
  assert.equal(C.setPlanned(p, "1", null).md[0].planned, undefined);
  assert.equal(C.findTask(tasks, "2").area, "job");
});

test("placeReason: any open task goes on the calendar (agents' tasks and project-plan tasks too); finished tasks never", () => {
  assert.equal(C.placeReason({ owner: "me", state: "todo", id: "t1" }), "");
  assert.equal(C.placeReason({ owner: "agents", state: "next", id: "t2" }), "");
  assert.equal(C.placeReason({ owner: "agents", state: "next", id: "p/t2", source: "plan.yaml" }), "");
  assert.match(C.placeReason({ owner: "me", state: "done", id: "t3" }), /finished/);
});

test("the agents' lane is parked: no agents helpers are exported from plan-core", () => {
  ["agentItemsOn", "agentSpans", "agentRows", "agentLaneCount", "weekAgentLayout", "agentsBusyReason", "MAX_AGENT_LANES"]
    .forEach((n) => assert.equal(C[n], undefined, n));
});

test("allDayTitleOn: an all-day event blocks its day for new events, not for itself", () => {
  const evs = [{ id: "a", day: "2026-10-09", title: "Trip" }, { id: "b", day: "2026-10-09", start: "10:00", end: "11:00", title: "Call" }];
  assert.equal(C.allDayTitleOn(evs, "2026-10-09", null), "Trip");
  assert.equal(C.allDayTitleOn(evs, "2026-10-09", "a"), "");
  assert.equal(C.allDayTitleOn(evs, "2026-10-10", null), "");
});
