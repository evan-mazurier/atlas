/* Pure helpers of the Ledger tasks table (no DOM, so node can test them). */

/** The task's own name: summary goal, session title, first helper label, else its id. */
export function taskTitle(t) {
  return t.title || (t.summary && t.summary.goal) || t.session_title || ((t.helpers || [])[0] && (t.helpers[0].labels || [])[0]) || t.id;
}

/** The linked Plan item's title (docs/v2/contracts.md 1.7 `plan_title`), "" when the task is not linked or it does not resolve. */
export function planTitle(t) {
  return t && typeof t.plan_title === "string" ? t.plan_title : "";
}

/** What the task-name cell shows: {prefix, text, hint}. A linked task shows "Plan:" + the Plan title (so the cost per
    Plan item reads at a glance) and keeps its own name as the hint; an unlinked one shows its own name. */
export function taskCellParts(t) {
  const pt = planTitle(t);
  return pt ? { prefix: "Plan:", text: pt, hint: taskTitle(t) } : { prefix: "", text: taskTitle(t), hint: "" };
}
