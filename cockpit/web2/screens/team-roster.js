/* Team roster (docs/v2/contracts.md §5.1, §8.4): the collapsible tables under the creature strip.
   Everything comes from GET /api/v2/shell/roster (agent files + workflows.yaml, read per request);
   this module only lays it out. rosterHtml is pure so the node tests can run it. */
import { esc, rosterName } from "../kit/format.js";

const KEY = "atlas.team.roster.open";
const ORDER = ["scout", "sweeper", "researcher", "implementer", "drafter", "test-runner", "reviewer", "senior-reviewer"];
const CHIEF = { id: "main session", model: "opus", use_for: "Main session: scopes the request, decides, splits the work, briefs the helpers, checks what comes back.", never: "Delegates decisions, architecture or API shape" };

export function readOpen() {
  try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; }
}
export function saveOpen(open) {
  try { localStorage.setItem(KEY, open ? "1" : "0"); } catch (e) { /* storage unavailable: the choice just is not remembered */ }
}

function rank(id) { const i = ORDER.indexOf(id); return i < 0 ? ORDER.length : i; }
function model(m) {
  const raw = String(m || "").toLowerCase();
  if (!raw) return "";
  const k = ["opus", "sonnet", "haiku"].find((x) => raw.includes(x));
  const col = k ? "var(--m-" + k + ")" : "var(--ink-2)";
  return `<span class="roster-model" style="color:${col}"><i style="background:${col}"></i>${esc(k ? k.charAt(0).toUpperCase() + k.slice(1) : raw)}</span>`;
}
function plain(t) { return String(t == null ? "" : t).replace(/\bOperator(?:'s)?\b/g, (m) => (m.endsWith("s") ? "the operator's" : "the operator")); }
function table(heads, rows, cls) {
  return `<div class="tscroll"><table class="tbl roster-tbl ${cls}"><thead><tr>${heads.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${
    rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

export function rosterHtml(d) {
  const helpers = [CHIEF].concat((d.helpers || []).slice().sort((a, b) => rank(a.id) - rank(b.id) || a.id.localeCompare(b.id)));
  const hRows = helpers.map((h) => [
    `<b>${esc(h === CHIEF ? "orchestrator" : rosterName(h.id))}</b>`, model(h.model),
    esc(plain(h.use_for)), esc(plain(h.never))]);
  const tRows = (d.types || []).map((t) => [
    `<b>${esc(t.label || t.id)}</b>`, esc((t.crew || []).map(rosterName).join(", ") || (t.id === "playbook" ? "set per playbook (below)" : "none")),
    t.budget == null ? "no rework" : esc(String(t.budget)), esc(t.rung == null ? "" : String(t.rung)), esc(plain(t.move))]);
  const pRows = (d.playbooks || []).map((p) => [`<span class="mono">${esc(p.command)}</span>`,
    esc(p.per_task ? "set per task, by its type" : ((p.crew || []).map(rosterName).join(", ") || "none")),
    p.per_task ? "set per task" : (p.budget == null ? "no rework" : esc(String(p.budget))), esc(p.area || "")]);
  return `<h4>Helpers</h4>${table(["Name", "Model", "Use it for", "Never"], hRows, "roster-helpers")}
    <h4>Types of work</h4>${table(["Type", "Crew", "Rework budget", "Rung", "Moves when"], tRows, "roster-types")}
    <h4>Playbooks</h4>${table(["Command", "Crew", "Rework budget", "Area"], pRows, "roster-playbooks")}`;
}

/** Mounts the toggle + body into `host`; loads the roster the first time it is opened. */
export function mountRoster(host, ctx) {
  host.className = "team-roster";
  host.innerHTML = `<button type="button" class="roster-toggle" aria-expanded="false"><span class="chev" aria-hidden="true"></span>Roster</button>
    <div class="roster-body" hidden></div>`;
  const btn = host.querySelector(".roster-toggle"), body = host.querySelector(".roster-body");
  let loaded = false;
  async function load() {
    loaded = true;
    body.textContent = "Loading the roster.";
    try {
      const d = await ctx.api.get("/api/v2/shell/roster");
      body.innerHTML = rosterHtml(d);
    } catch (e) {
      loaded = false;
      body.textContent = "The roster could not be read (" + (e && e.message || e) + ").";
    }
  }
  function set(open, save) {
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    body.hidden = !open;
    if (save) saveOpen(open);
    if (open && !loaded) load();
  }
  btn.addEventListener("click", () => set(body.hidden, true));
  set(readOpen(), false);
}
