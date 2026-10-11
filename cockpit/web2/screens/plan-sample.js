/* Dev aid: `#/plan?sample=1` lays a few finished reviews over the real payload when it has none,
   so the review tray can be checked before the ledger has such runs. Nothing here is saved. */
export function sampleOverlay(d, today) {
  const out = Object.assign({}, d);
  if (!(d.review_tray || []).length) {
    out.review_tray = [
      { id: "rv_s1", task_id: "s1", title: "Build Product page", area: "md", review_min: 25, sample: true, verdict: "READY", ready_at: today + "T11:36:00", scheduled: null },
      { id: "rv_s9", task_id: "s9", title: "Notes batch 2", area: "job", review_min: 15, sample: true, verdict: "READY", ready_at: today + "T09:00:00", scheduled: null },
    ];
  }
  return out;
}
