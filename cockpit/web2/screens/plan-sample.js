/* Dev aid: `#/plan?sample=1` lays a few memos (one open, one scheduled) over the real payload when it has no
   tasks.yaml task, so the To schedule rail can be checked on an empty plan. Nothing here is saved. */
export function sampleOverlay(d, today) {
  const tasks = d.tasks || {};
  if (Object.keys(tasks).some((a) => (tasks[a] || []).some((t) => t.source === "tasks.yaml"))) return d;
  const area = (d.areas && d.areas[0] && d.areas[0].id) || "md";
  const out = Object.assign({}, d, { tasks: Object.assign({}, tasks) });
  out.tasks[area] = (tasks[area] || []).concat([
    { id: "s_memo1", title: "Sample: outline the follow-up", state: "todo", owner: "me", source: "tasks.yaml", project: null, estimate_min: 90, reversible: true, sample: true },
    { id: "s_memo2", title: "Sample: already scheduled", state: "todo", owner: "me", source: "tasks.yaml", project: null, estimate_min: 30, reversible: true, sample: true,
      planned: { day: today, start: "15:00", end: "15:30", on: "me" } },
  ]);
  return out;
}
