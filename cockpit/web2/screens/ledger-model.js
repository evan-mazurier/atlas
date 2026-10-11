/* Pure helpers of the Ledger tasks table (no DOM, so node can test them). */

/** The task's own name: the API's `title` (Plan item, else summary goal, else first helper label, else session title),
    with the same chain as a fallback for records without it, else its id. */
export function taskTitle(t) {
  return t.title || (t.summary && t.summary.goal) || ((t.helpers || [])[0] && (t.helpers[0].labels || [])[0]) || t.session_title || t.id;
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

/** Plain words for a task status. */
const STATUS_WORDS = { open: "in progress", done: "closed", ended: "session ended", switched: "next task started" };
export function statusWord(s) { return STATUS_WORDS[s] || s || "in progress"; }

/** Rework rounds of a task: {main, sub}. main = "{own} of {budget}" (+ " +N granted"), sub = "{n} carried over" or "".
    The rounds counted are this task's own; rounds carried from the previous unresolved task are listed apart. */
export function roundsParts(r) {
  if (!r) return { main: "–", sub: "" };
  const own = r.own != null ? r.own : r.needs_changes || 0;
  const carried = r.carried || 0;
  let main = r.budget == null ? (own ? own + " (no budget)" : "no budget") : own + " of " + r.budget;
  if ((r.granted || 0) > 0) main += " +" + r.granted + " granted";
  return { main, sub: carried > 0 ? carried + " carried over" : "" };
}

/** The Verdict column: [glyph, tone, label]. A refusal shows only when refusals exist and the task is not READY. */
export function verdictParts(t) {
  const refused = (t.refusals || []).some((x) => x.would === false);
  if (refused && t.final_verdict !== "READY") return ["×", "crit", "Refused, over budget"];
  if (t.unverified_flag) return ["!", "warn", "Unverified, no passing review"];
  if (t.final_verdict === "READY") return ["✓", "ok", "READY" + (t.status === "done" ? " · closed" : "")];
  if (t.final_verdict === "NEEDS_CHANGES") {
    const left = t.rounds && t.rounds.left != null ? " · " + t.rounds.left + " left" : "";
    return ["!", "warn", "NEEDS CHANGES" + left];
  }
  if (t.final_verdict === "ESCALATE") return ["!", "crit", "ESCALATED"];
  return ["–", "muted", statusWord(t.status)];
}

/** A matched path for display: single forward slashes (the ledger keeps it with doubled backslashes). */
export function displayPath(p) { return String(p == null ? "" : p).replace(/[\\/]+/g, "/"); }

/** Header tooltips of the task table. */
export const TASK_HEAD_TIPS = {
  Task: "What the task is called: the linked Plan item, else its summary goal, else the first description given to a helper, else the session title.",
  Area: "The area of work the task belongs to.",
  Type: "The type of work the session declared for this task.",
  Crew: "Helpers launched in the task.",
  "Rework rounds": "NEEDS CHANGES verdicts in this task against its budget; granted = rounds the operator added with /grant-rework; carried = rounds still open from the previous unresolved task in the same session.",
  Caught: "Findings the reviewers raised.",
  Usage: "Price-weighted tokens, Sonnet = 1.",
  Verdict: "The latest review verdict, or the task status when there is none.",
};
