"""
Atlas dispatcher — the job queue between the control plane and the executor.

The dashboard/assistant (WSL) ENQUEUE jobs here as JSON files; the Windows worker
(worker.py) picks them up, runs a headless Claude Code session in a sandbox, and
writes the result back into the same file. Files are the contract — same idea as
status.yaml — so the WSL↔Windows boundary is crossed purely through the shared disk.

This module only reads the registry + reads/writes job JSON. The worker needs none
of this (it works off the job JSON alone, stdlib only).
"""
from __future__ import annotations

import os
import re
import json
import ntpath
import secrets
import uuid
import datetime as dt
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent      # D:\work\atlas  /  /mnt/d/AI/Atlas
JOBS_DIR = ATLAS_ROOT / "jobs"
REGISTRY = ATLAS_ROOT / "registry.yaml"
SCHEDULE = ATLAS_ROOT / "schedule.yaml"                  # Atlas-owned global schedule (docs/scheduling.md)
PROCEDURES = ATLAS_ROOT / "procedures.yaml"

# Canonical session lifecycle (LOCKED — shared contract between the dispatch engine and
# the Stage-5 control center viz; don't rename these). A Stage-4 job flows:
#   queued → scoping → [the operator confirms contract] → coding → testing → review
#                         │                          ▲              │
#                         │                          └─ reworking ◄─┘ (needs-changes, ≤ budget)
#   any blocking decision → needs_input (the operator answers → resume)
#   gate passed → ready (committed on atlas/<id>) → [the operator merges] → merged
#   hard test fail after budget / reviewer escalate → failed
# NOTE: pre-gate-engine (Stage 3) exec jobs still terminate as "done" (single coding run,
# manual diff review). Stage 4 replaces that tail with testing→review→ready/merged.
LIFECYCLE = ("queued", "scoping", "coding", "testing", "review",
             "reworking", "needs_input", "ready", "merged", "failed", "done")
TERMINAL = {"ready", "merged", "failed", "done"}
ACTIVE_STATES = {s for s in LIFECYCLE if s not in TERMINAL}   # non-terminal → lights a node

# Scoping is uniform across projects (interrogation quality matters more than cost): always
# Opus / high effort, read-only. Coding/exec model+effort still come from procedures.yaml.
# Env-overridable (like the reviewer) so this role is config, not a code edit — model-agnostic
# by design (docs/model-agnosticism.md Phase 3).
SCOPE_MODEL = os.environ.get("ATLAS_SCOPE_MODEL", "opus")
SCOPE_EFFORT = os.environ.get("ATLAS_SCOPE_EFFORT", "high")

# The architect (global agent) designs a project's org. Reflection tier → Opus / high.
ARCH_MODEL = os.environ.get("ATLAS_ARCH_MODEL", "opus")
ARCH_EFFORT = os.environ.get("ATLAS_ARCH_EFFORT", "high")

# The planner (global agent) proposes/maintains a project's plan.yaml. Same reflection
# tier as the architect (docs/planner.md §6.4).
PLANNER_MODEL = os.environ.get("ATLAS_PLANNER_MODEL", "opus")
PLANNER_EFFORT = os.environ.get("ATLAS_PLANNER_EFFORT", "high")

# Gate reviewer (Stage 4 Phase 4): the JUDGMENT gate — it catches what the tests can't, so it runs on
# Opus/high (a judgment role, like the scoper/architect). It can run up to 3×/job in the rework loop,
# but a clean job runs it once. Override per project with a `reviewer: {model, effort}` key in
# procedures.yaml, or globally via env.
REVIEW_MODEL = os.environ.get("ATLAS_REVIEW_MODEL", "opus")
REVIEW_EFFORT = os.environ.get("ATLAS_REVIEW_EFFORT", "high")


def resolve_review(project: str) -> dict:
    """Reviewer model/effort for the gate. A per-project `reviewer:` key in procedures.yaml
    overrides the sonnet/high default. (Distinct from the `review` *session type*, which is
    unrelated ad-hoc work.)"""
    import yaml
    p = yaml.safe_load(PROCEDURES.read_text(encoding="utf-8")) or {}
    r = ((p.get("projects") or {}).get(project, {}) or {}).get("reviewer") or {}
    return {"model": r.get("model", REVIEW_MODEL), "effort": r.get("effort", REVIEW_EFFORT)}


def _to_local(win_path: str) -> str:
    """Map a Windows D:\\path to /mnt/d/... when running on posix (WSL), so we can stat it."""
    if os.name != "nt":
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", win_path)
        if m:
            return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"
    return win_path


def projects() -> dict:
    """id -> {cwd, sidecar_dir, is_git, test_cmd} (Windows paths, for the worker).

    `cwd` is where dispatched CODE work runs — the git repo the worktree + test_cmd operate
    in: the registry `code_path` when set, else the dir holding status.yaml. `sidecar_dir` is
    where the Atlas sidecars live (status.yaml, plan.yaml) = the status.yaml dir. For a normal
    project the two are identical; a split-repo project (e.g. Almanac: code in the Foundry repo,
    status/plan in the vault) separates them, so post-merge status/plan bumps still land in the
    vault while the gate runs against the code. `test_cmd` is the hard-gate command (or None
    for targets without tests, e.g. vaults)."""
    import yaml
    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    out = {}
    for e in reg.get("projects", []):
        sidecar_dir = ntpath.dirname(e.get("status_file", ""))
        cwd_win = e.get("code_path") or sidecar_dir
        is_git = os.path.isdir(os.path.join(_to_local(cwd_win), ".git"))
        out[e["id"]] = {"cwd": cwd_win, "sidecar_dir": sidecar_dir,
                        "is_git": is_git, "test_cmd": e.get("test_cmd")}
    return out


def resolve_procedure(project: str, session_type: str | None = None) -> dict:
    """Return {model, effort, desc, types} for a project/session-type from procedures.yaml.
    Falls back to defaults when the session type is unknown."""
    import yaml
    p = yaml.safe_load(PROCEDURES.read_text(encoding="utf-8")) or {}
    d = p.get("defaults", {}) or {}
    model, effort, desc = d.get("model", "sonnet"), d.get("effort", "medium"), None
    proj = (p.get("projects") or {}).get(project, {}) or {}
    st = proj.get(session_type or "", {}) or {}
    if st:
        model, effort, desc = st.get("model", model), st.get("effort", effort), st.get("desc")
    return {"model": model, "effort": effort, "desc": desc, "types": list(proj.keys())}


def session_types() -> dict:
    """project -> [session type names], for the assistant to scope against."""
    import yaml
    p = yaml.safe_load(PROCEDURES.read_text(encoding="utf-8")) or {}
    return {k: list((v or {}).keys()) for k, v in (p.get("projects") or {}).items()}


def _job_path(jid: str) -> Path:
    return JOBS_DIR / f"{jid}.json"


def _write(job: dict) -> None:
    JOBS_DIR.mkdir(exist_ok=True)
    _job_path(job["id"]).write_text(json.dumps(job, indent=2), encoding="utf-8")


def enqueue(project: str, task: str, session_type: str | None = None,
            model: str | None = None, effort: str | None = None,
            permission_mode: str | None = None, scope: bool = False,
            run_in: str | None = None, read_only: bool = False,
            owner: str | None = None, plan_task: str | None = None) -> dict:
    """Create a queued job. `task` is the brief the worker hands to Claude (for a scope
    job, the raw goal the scoping session will refine into a contract).

    scope=True starts with an interactive **scoping** session (Opus/high): it interrogates
    The operator via the needs_input/resume loop and produces an implementation contract
    {brief, acceptance_criteria, test_cmd} for the operator to confirm (gate #1) before any code is
    merged. It runs in the job's disposable worktree (isolation is the safety boundary, so
    we avoid plan mode — which would hijack the output into an ExitPlanMode plan instead of
    the CONTRACT convention). scope=False is a direct exec job (model/effort from the
    session type in procedures.yaml).

    run_in selects the working dir for a SPLIT-REPO project (e.g. Almanac: code in the Foundry
    repo, sidecars in the vault). 'code' (default) → the code repo, with the worktree + test
    gate — for code-change work. 'sidecar' → the vault (sidecar_dir), run IN PLACE (no
    worktree, no test gate — the vault isn't git), for bookkeeping procedures that read/write
    status.yaml + log.md. For a single-repo project the two dirs are identical, so run_in is a
    no-op.

    permission_mode: defaults to 'skip' (autonomous, contained by the worktree/backup
    sandbox) for both scope and exec jobs; pass a value to override.

    plan_task is the plan.yaml task id this job was launched FROM (a planned-task launch). It is
    written back into that task as `dispatch: <job id>` — the key `_advance_plan_after_merge`
    matches on, so merging the job advances the task and recomputes next_target. A free-form
    dispatch has no plan task and passes None, which leaves plan.yaml untouched."""
    p = projects().get(project)
    if not p:
        raise ValueError(f"unknown project: {project!r}")
    proc = resolve_procedure(project, session_type)
    rev = resolve_review(project)
    jid = "job_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
    target = run_in or "code"
    if target == "sidecar":                # run in the vault, in place (no worktree, no gate)
        job_cwd = p.get("sidecar_dir") or p["cwd"]
        job_is_git = os.path.isdir(os.path.join(_to_local(job_cwd), ".git"))
        job_test_cmd = None                # vault bookkeeping is never code-gated
    else:                                  # 'code' (default): the code repo + its worktree/gate
        job_cwd, job_is_git, job_test_cmd = p["cwd"], p["is_git"], p.get("test_cmd")
    job = {
        "id": jid,
        "project": project,
        "cwd": job_cwd,                    # where the session runs (code repo or vault, per run_in)
        "sidecar_dir": p.get("sidecar_dir") or p["cwd"],  # status.yaml/plan.yaml always live here
        "run_in": target,                  # 'code' | 'sidecar' (split-repo routing)
        "read_only": bool(read_only),      # a report procedure → no gate/review, present result + dismiss
        "owner": owner,                    # the procedure's owning agent id → lights the REAL agent in the live org
        "is_git": job_is_git,
        "task": task,
        "session_type": session_type,
        "kind": "scope" if scope else "exec",
        "scope": scope,                    # True => begins with an interactive scoping session
        "model": model or (SCOPE_MODEL if scope else proc["model"]),
        "effort": effort or (SCOPE_EFFORT if scope else proc["effort"]),
        "status": "queued",                # see LIFECYCLE
        "permission_mode": permission_mode or "skip",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "started": None,
        "finished": None,
        "sandbox": None,                   # worktree path (git) or backup path (vault)
        "branch": None,                    # atlas/<id> for git repos
        "session_id": None,                # for --resume on escalation / scoping interrogation
        "result": None,
        "diffstat": None,
        "error": None,
        "question": None,                  # set when status=needs_input
        "answer": None,                    # the operator's reply, fed to --resume
        "resume": False,                   # True => worker resumes the session vs. a fresh run
        "discard": False,                  # True => worker cleans the worktree/backup + deletes record
        "worktree_cleaned": False,         # True => worktree reclaimed (record kept) by sweep_worktrees
        "cost_usd": None,                  # kept internally; the UI shows tokens instead
        "tokens": 0,                       # total tokens consumed across the job's runs
        # ---- Stage 4 (gate engine) contract + state ----
        "contract": None,                  # {brief, acceptance_criteria, test_cmd} from scoping
        "contract_confirmed": False,       # set by confirm_contract() — human gate #1
        "acceptance_criteria": None,       # denormalized from the confirmed contract
        "test_cmd": job_test_cmd,          # hard-gate command (None => review-only gate; always None for run_in=sidecar)
        "rework_count": 0,                 # reviewer rework attempts used (budget in worker)
        "review_model": rev["model"],      # gate reviewer model/effort (sonnet/high default)
        "review_effort": rev["effort"],
        "review_verdict": None,            # commit-ready | needs-changes | escalate
        "review_notes": None,              # reviewer findings (fed back on rework)
        "test_output": None,               # tail of the last test_cmd run
        "commit": None,                    # sha once committed on atlas/<id> (status ready)
        "plan_task": (plan_task or None),  # plan.yaml task this was launched from, if any
    }
    _write(job)
    if plan_task:
        _link_plan_task(project, plan_task, jid)
    return job


def _link_plan_task(project_id: str, task_id: str, jid: str) -> bool:
    """Write `dispatch: <jid>` into the plan.yaml task the job was launched from.

    This is the link `worker._advance_plan_after_merge` matches on. Without it a merge cannot
    tell which task it just completed, so the task never advances and next_target keeps pointing
    at finished work until someone fixes it by hand.

    Best-effort by design: a plan.yaml that is missing, unwritable, or has no such task must
    never block the dispatch itself — the job is already on disk and valid without the link.
    """
    try:
        def mut(block, indent):
            return _set_task_scalar(block, "dispatch", jid, indent)
        return _edit_plan_task(project_id, task_id, mut)
    except Exception as e:  # noqa: BLE001 — never fail a dispatch over a bookkeeping link
        print(f"  ! plan.yaml dispatch-link skipped for {project_id}/{task_id}: {e}")
        return False


def _project_procedures_file(project_id: str) -> str | None:
    import yaml
    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    for e in reg.get("projects", []):
        if e.get("id") == project_id:
            return e.get("procedures_file")
    return None


def _project_plan_file(project_id: str) -> str | None:
    import yaml
    reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    for e in reg.get("projects", []):
        if e.get("id") == project_id:
            return e.get("plan_file")
    return None


def next_planned_task(project_id: str) -> dict | None:
    """The deterministic bridge from plan → dispatch (docs/planner.md §6.6). Returns the project's
    single top `state: next` task with its active-milestone context, or None (no plan / nothing next).
    Used BOTH to TELL (the receptionist's grounding — "what's next") and to ACT (dispatch_next_task).
    Shape: {id, title, notes, type, priority, milestone, milestone_title}. Defensive: any read/parse
    problem → None (never raises)."""
    import yaml
    pf = _project_plan_file(project_id)
    if not pf:
        return None
    try:
        data = yaml.safe_load(Path(_to_local(pf)).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — missing/unreadable plan → nothing next
        return None
    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    nxt = next((t for t in tasks if t.get("state") == "next"), None)
    if not nxt:
        return None
    ms = {m.get("id"): m for m in (data.get("milestones") or []) if isinstance(m, dict)}
    mid = nxt.get("milestone")
    return {
        "id": nxt.get("id"),
        "title": nxt.get("title") or nxt.get("id") or "",
        "notes": nxt.get("notes") or "",
        "type": nxt.get("type") or "dispatch",       # dispatch | manual
        "priority": nxt.get("priority") or "medium",
        "milestone": mid,
        "milestone_title": (ms.get(mid) or {}).get("title") if mid else None,
    }


def brief_from_task(nt: dict) -> str:
    """Turn a resolved plan task into a dispatch brief (its title + notes + milestone context), so a
    launched-from-plan task carries the planner's own words into scoping — no re-typing."""
    parts = [nt.get("title") or ""]
    if nt.get("notes"):
        parts += ["", nt["notes"]]
    if nt.get("milestone_title"):
        parts += ["", f"(Milestone: {nt['milestone_title']})"]
    return "\n".join(parts).strip()


def enqueue_procedure(project_id: str, procedure_id: str) -> dict:
    """Run a project's pre-defined, repeatable procedure: look up its strict `instructions` in the
    project's procedures.yaml and enqueue them as a direct exec job (scope=False — a procedure is
    never-changing, so no scoping). model/effort resolve from the procedure's session_type."""
    import yaml
    pf = _project_procedures_file(project_id)
    if not pf:
        raise ValueError(f"project {project_id!r} has no procedures_file")
    data = yaml.safe_load(Path(_to_local(pf)).read_text(encoding="utf-8")) or {}
    proc = next((p for p in (data.get("procedures") or [])
                 if isinstance(p, dict) and p.get("id") == procedure_id), None)
    if not proc:
        raise ValueError(f"procedure {procedure_id!r} not found in {pf}")
    instr = (proc.get("instructions") or proc.get("summary") or proc.get("name") or "").strip()
    task = f"Run the procedure '{proc.get('name') or procedure_id}'.\n\n{instr}"
    # Split-repo routing: a procedure may declare `runs_in: code|sidecar`. Default: 'sidecar'
    # for a split-repo project (procedures are mostly vault bookkeeping — status/log), else
    # 'code' (single-repo → the two dirs coincide, so it's a no-op). A code-repo procedure in a
    # split-repo project (e.g. run-pytest-gate) must declare `runs_in: code` to override.
    pr = projects().get(project_id) or {}
    split_repo = bool(pr.get("sidecar_dir") and pr.get("sidecar_dir") != pr.get("cwd"))
    run_in = proc.get("runs_in") or ("sidecar" if split_repo else "code")
    return enqueue(project_id, task, session_type=proc.get("session_type"), scope=False,
                   run_in=run_in, read_only=bool(proc.get("read_only")), owner=proc.get("owner"))


def enqueue_architect(project_id: str, project_path: str, goal: str,
                      project_name: str | None = None, is_new: bool = False,
                      subject_kind: str = "project") -> dict:
    """Queue an ARCHITECT job: an interactive Opus/high session that designs the org for a
    SUBJECT — a project (subject_kind='project', default), a new Atlas capability
    (subject_kind='capability', e.g. project_id='atlas-core', path=the Atlas repo), OR an
    umbrella middle tier (subject_kind='umbrella', e.g. project_id='orchard' → cwd
    umbrellas/orchard/, the agents shared across a project's sub-projects). Unlike
    enqueue(), it takes an explicit path (the subject may not be in the registry yet) and uses
    NO worktree — the architect is read-only/propose-only and runs in the subject's dir; the
    files are written only after the operator approves the proposal (approve_architect). Reuses the
    needs_input/resume loop to interrogate."""
    if subject_kind == "capability" and not project_path:
        project_path = str(ATLAS_ROOT)     # a capability's subject dir IS the Atlas repo itself
    if subject_kind == "umbrella" and not project_path:
        # an umbrella's org lives INSIDE Atlas at umbrellas/<id>/ (mirrors the brain roster);
        # cwd = that dir, so the normal cwd-write path in approve_architect writes it correctly.
        project_path = str(ATLAS_ROOT / "umbrellas" / project_id)
    if not (project_id and project_path and (goal or "").strip()):
        raise ValueError("architect needs project_id, project_path, and a goal")
    jid = "job_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
    job = {
        "id": jid,
        "project": project_id,
        "project_name": project_name or project_id,
        "cwd": project_path,
        "is_git": False,                   # architect uses no worktree (read-only/propose-only)
        "task": goal.strip(),
        "session_type": None,
        "kind": "architect",
        "scope": False,
        "subject_kind": subject_kind,      # 'project' (default) | 'capability' (Atlas component) | 'umbrella' (middle tier)
        "is_new": bool(is_new),            # new project → also create status.yaml + registry entry
        "model": ARCH_MODEL,
        "effort": ARCH_EFFORT,
        "status": "queued",
        "permission_mode": "skip",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "started": None, "finished": None,
        "sandbox": None, "branch": None, "session_id": None,
        "result": None, "diffstat": None, "error": None,
        "question": None, "answer": None, "resume": False, "discard": False,
        "worktree_cleaned": False, "cost_usd": None, "tokens": 0,
        "proposal": None,                  # {agents_yaml, procedures_yaml} once the architect proposes
        "proposal_written": False,
        # gate fields unused by the architect, kept for schema parity
        "contract": None, "contract_confirmed": False, "acceptance_criteria": None,
        "test_cmd": None, "rework_count": 0, "review_model": None, "review_effort": None,
        "review_verdict": None, "review_notes": None, "test_output": None, "commit": None,
    }
    _write(job)
    return job


VAULT_REPO_WIN = os.environ.get("ATLAS_VAULT", r"D:\work\orchard-vault")   # the Orchard vault git repo the vault-keeper writes


def _vault_dirty(root: str) -> bool:
    """True if the vault working tree has uncommitted changes (so a vault-keeper draft would
    mingle with them). We refuse to start on a dirty tree so the captured diff = only the draft."""
    import subprocess
    try:
        cp = subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True)
        return bool((cp.stdout or "").strip())
    except Exception:  # noqa: BLE001 — if git is unavailable, don't block (worker will surface real errors)
        return False


def enqueue_vaultkeeper(request: str, mode: str = "manual") -> dict:
    """Queue a VAULT-KEEPER job: an interactive Opus/high session that drafts a change to the
    the Orchard vault decision vault (or the north-star), PROPOSE-ONLY. Runs IN the vault repo (no worktree —
    it edits wiki/ in place, uncommitted); the worker captures the git diff + runs the deterministic
    vault-lint as the proposal. Nothing is committed until the operator approves (approve_vaultkeeper);
    reject (discard_vaultkeeper) reverts the working tree. Refuses to start on a dirty vault so the
    draft diff is clean."""
    if not (request or "").strip():
        raise ValueError("vault-keeper needs a request")
    root = _to_local(VAULT_REPO_WIN)
    if _vault_dirty(root):
        raise ValueError("the Orchard vault has uncommitted changes — commit or stash them first so "
                         "the vault-keeper's draft stays isolated")
    jid = "job_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
    job = {
        "id": jid,
        "project": "orchard",                 # the umbrella subject
        "project_name": "Orchard vault",
        "cwd": VAULT_REPO_WIN,
        "is_git": True,
        "task": request.strip(),
        "session_type": None,
        "kind": "vault-keeper",
        "scope": False,
        "vk_mode": mode,                     # 'manual' (the operator asked) | 'auto' (post-merge — a later slice)
        "model": ARCH_MODEL,                 # opus / high, matching the vault-keeper agent def
        "effort": ARCH_EFFORT,
        "status": "queued",
        "permission_mode": "skip",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "started": None, "finished": None,
        "sandbox": None, "branch": None, "session_id": None,
        "result": None, "diffstat": None, "error": None,
        "question": None, "answer": None, "resume": False, "discard": False,
        "worktree_cleaned": False, "cost_usd": None, "tokens": 0,
        "proposal": None,                    # {summary, files, diff, lint} once it proposes
        "proposal_written": False,
        "contract": None, "contract_confirmed": False, "acceptance_criteria": None,
        "test_cmd": None, "rework_count": 0, "review_model": None, "review_effort": None,
        "review_verdict": None, "review_notes": None, "test_output": None, "commit": None,
    }
    _write(job)
    return job


def approve_vaultkeeper(jid: str, message: str = "") -> dict:
    """Human gate: the operator approved the vault-keeper's drafted change. COMMIT the working-tree changes
    in the Orchard vault repo (the vault-keeper drafted them there, uncommitted). The agent never
    commits — Atlas does, on approval. Returns {ok, note, commit}."""
    import subprocess
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    if job.get("kind") != "vault-keeper":
        raise ValueError(f"job {jid} is not a vault-keeper job")
    root = _to_local(job["cwd"])
    st = subprocess.run(["git", "-C", root, "status", "--porcelain"], capture_output=True, text=True)
    if not (st.stdout or "").strip():
        raise ValueError("no drafted changes to commit (the vault working tree is clean)")
    msg = (message or "").strip() or f"vault-keeper: {(job.get('task') or 'update the vault')[:72]}"
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    cp = subprocess.run(["git", "-C", root, "commit", "-m", msg + "\n\nApproved by the operator via the Atlas vault-keeper."],
                        capture_output=True, text=True)
    if cp.returncode != 0:
        raise ValueError(f"git commit failed: {(cp.stderr or cp.stdout)[:400]}")
    rev = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    job["proposal_written"] = True
    job["commit"] = rev
    job["status"] = "done"
    job["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    _write(job)
    return {"ok": True, "note": f"committed to the vault ({rev})", "commit": rev}


def discard_vaultkeeper(jid: str) -> dict:
    """Reject the vault-keeper's draft: REVERT the vault working tree (restore tracked edits +
    remove newly-created files under wiki/) so nothing is left behind, and finish the job. Safe
    because enqueue refused to start on a dirty tree, so the only changes present are the draft."""
    import subprocess
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    if job.get("kind") != "vault-keeper":
        raise ValueError(f"job {jid} is not a vault-keeper job")
    root = _to_local(job["cwd"])
    subprocess.run(["git", "-C", root, "reset", "-q"], capture_output=True, text=True)          # unstage any intent-to-add
    subprocess.run(["git", "-C", root, "checkout", "--", "."], capture_output=True, text=True)  # revert tracked edits
    subprocess.run(["git", "-C", root, "clean", "-fd"], capture_output=True, text=True)         # remove new files anywhere (matches approve's add -A scope)
    job["discard"] = True
    job["status"] = "done"
    job["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    _write(job)
    return {"ok": True, "note": "draft discarded; vault working tree reverted"}


def enqueue_planner(project_id: str, project_path: str, goal: str, mode: str = "plan",
                     task_id: str | None = None) -> dict:
    """Queue a PLANNER job: an interactive Opus/high session that proposes/maintains a
    project's plan.yaml (milestones + tasks). Mirrors enqueue_architect — no worktree (the
    planner is read-only/propose-only over structured state; it never reads a project's
    prose), and the file is written only after the operator approves (approve_planner). The
    project already exists when planned, so unlike the architect there is no `is_new` path.

    `mode` selects which of the planner's THREE capabilities this job runs (docs/scheduling.md §7 —
    the SAME agent, not a new one):
      • 'plan'       — per-project scope: propose that project's plan.yaml.
      • 'schedule'   — GLOBAL scope: fit every project's ready tasks into the operator's free time and
                       propose Atlas's schedule.yaml. project_id/_path are the Atlas repo itself.
      • 'reestimate' — per-project, per-task scope: given one task's current numbers (estimate,
                       remaining, sessions done) + the operator's free-text note (`goal`), propose a
                       revised `remaining` for that task (`task_id`, required for this mode).
    """
    if mode not in ("plan", "schedule", "reestimate"):
        raise ValueError(f"unknown planner mode: {mode!r}")
    if not (project_id and project_path and (goal or "").strip()):
        raise ValueError("planner needs project_id, project_path, and a goal")
    if mode == "reestimate" and not (task_id or "").strip():
        raise ValueError("reestimate mode needs a task_id")
    jid = "job_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
    job = {
        "id": jid,
        "project": project_id,
        "cwd": project_path,
        "is_git": False,                   # planner uses no worktree (read-only/propose-only)
        "task": goal.strip(),
        "session_type": None,
        "kind": "planner",
        "mode": mode,                      # 'plan' (per-project) | 'schedule' (global) |
                                            # 'reestimate' (per-task) — §7
        "task_id": task_id.strip() if mode == "reestimate" else None,
        "scope": False,
        "model": PLANNER_MODEL,
        "effort": PLANNER_EFFORT,
        "status": "queued",
        "permission_mode": "skip",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "started": None, "finished": None,
        "sandbox": None, "branch": None, "session_id": None,
        "result": None, "diffstat": None, "error": None,
        "question": None, "answer": None, "resume": False, "discard": False,
        "worktree_cleaned": False, "cost_usd": None, "tokens": 0,
        "proposal": None,                  # {summary, plan_yaml} once the planner proposes
        "proposal_written": False,
        # gate fields unused by the planner, kept for schema parity
        "contract": None, "contract_confirmed": False, "acceptance_criteria": None,
        "test_cmd": None, "rework_count": 0, "review_model": None, "review_effort": None,
        "review_verdict": None, "review_notes": None, "test_output": None, "commit": None,
    }
    _write(job)
    return job


def _ensure_plan_file_registry(project_id: str, plan_win_path: str) -> None:
    """Add a `plan_file:` line to a project's registry.yaml entry if it doesn't already
    have one (text append, so comments/formatting elsewhere in the file survive)."""
    import yaml
    reg_txt = REGISTRY.read_text(encoding="utf-8")
    existing = yaml.safe_load(reg_txt) or {}
    for e in (existing.get("projects") or []):
        if e.get("id") == project_id and e.get("plan_file"):
            return                          # already registered
    bs = chr(92)  # backslash; YAML double-quoted strings need it doubled
    dq = '"' + plan_win_path.replace(bs, bs + bs) + '"'
    lines = reg_txt.splitlines(keepends=True)
    out, in_block, done = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"- id: {project_id}"):
            in_block, done = True, False
        elif in_block and stripped.startswith("- id:"):
            out.append(f"    plan_file: {dq}\n")
            in_block, done = False, True
        elif in_block and not stripped and not done:
            # blank line ends the block on some entries; insert before it
            out.append(f"    plan_file: {dq}\n")
            in_block, done = False, True
        out.append(line)
    if in_block and not done:                # block ran to EOF without a trailing blank line
        out.append(f"    plan_file: {dq}\n")
        done = True
    if done:
        REGISTRY.write_text("".join(out), encoding="utf-8")


def _ensure_status_and_registry(job: dict) -> None:
    """For a NEW project: write a starter status.yaml (if absent) and append a registry entry
    (preserving the file's comments via a text append; skipped if the id is already present)."""
    import yaml
    root = Path(_to_local(job["cwd"]))
    pid = job["project"]
    name = job.get("project_name") or pid
    sf = root / "status.yaml"
    if not sf.exists():
        goal1 = ((job.get("task") or "").strip().splitlines() or [""])[0][:120]
        sf.write_text(
            f'project: "{name}"\n'
            "kind: app\n"
            "status: wiring\n"
            'phase: "org wired — ready to dispatch"\n'
            "health: amber\n"
            f"updated: {dt.date.today().isoformat()}\n"
            'last_session: "org defined by the architect"\n'
            f'next_target: "{goal1}"\n'
            'detail: "PLAN.md"\n'
            "metrics: {}\n"
            "blockers: []\n", encoding="utf-8")
    reg_txt = REGISTRY.read_text(encoding="utf-8")
    existing = yaml.safe_load(reg_txt) or {}
    ids = {e.get("id") for e in (existing.get("projects") or [])}
    if pid in ids:
        return
    win = job["cwd"].rstrip("\\/").replace("/", "\\")
    bs = chr(92)  # backslash; YAML double-quoted strings need it doubled

    def dq(name_):
        return '"' + (win + bs + name_).replace(bs, bs + bs) + '"'
    block = (f"\n  - id: {pid}\n"
             f"    status_file: {dq('status.yaml')}\n"
             f"    procedures_file: {dq('procedures.yaml')}\n"
             f"    agents_file: {dq('agents.yaml')}\n")
    if not reg_txt.endswith("\n"):
        reg_txt += "\n"
    REGISTRY.write_text(reg_txt + block, encoding="utf-8")


def approve_architect(jid: str, agents_yaml: str, procedures_yaml: str | None = None) -> dict:
    """Human gate: the operator approves (and may have edited) the architect's proposal. Validate the
    YAML, write agents.yaml (+ procedures.yaml) into the subject's cwd, register a new project
    (project subject only), and mark the job done. The architect itself never writes files —
    Atlas does, on approval. Umbrella subjects write to umbrellas/<id>/ via the same cwd path;
    only 'capability' is unwired (its cwd = the Atlas repo root would clobber the brain roster)."""
    import yaml
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    if job.get("kind") != "architect":
        raise ValueError(f"job {jid} is not an architect job")
    if job.get("subject_kind") == "capability":
        # A capability's approved output belongs in automation-map.yaml + a capability procedures
        # file, NOT dumped into job['cwd']/agents.yaml (which for atlas-core IS the brain roster).
        # That routing is a separate slice; until it exists, refuse to avoid clobbering the brain.
        raise ValueError("capability-subject approve is not wired yet — read the proposal to "
                         "validate; capability approve-routing (automation-map.yaml) is the next slice")
    ay = (agents_yaml or "").strip()
    py_ = (procedures_yaml or "").strip()
    if not ay:
        raise ValueError("agents.yaml is empty")
    try:
        if not isinstance(yaml.safe_load(ay), dict):
            raise ValueError("agents.yaml is not a mapping")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"agents.yaml is not valid YAML: {e}")
    if py_:
        try:
            yaml.safe_load(py_)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"procedures.yaml is not valid YAML: {e}")
    root = Path(_to_local(job["cwd"]))
    root.mkdir(parents=True, exist_ok=True)
    (root / "agents.yaml").write_text(ay + "\n", encoding="utf-8")
    note = "agents.yaml"
    if py_:
        (root / "procedures.yaml").write_text(py_ + "\n", encoding="utf-8")
        note += " + procedures.yaml"
    note += f" written to {job['cwd']}"
    if job.get("is_new") and job.get("subject_kind", "project") == "project":
        _ensure_status_and_registry(job)   # registry is for projects only (umbrellas live in umbrellas/)
        note += "; registered in registry.yaml"
    job["proposal"] = {"agents_yaml": ay, "procedures_yaml": py_}
    job["proposal_written"] = True
    job["status"] = "done"
    job["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    _write(job)
    return {"ok": True, "note": note}


def _schedmath():
    """Lazy import of schedmath.py (the pure collision-guard math, Contract C1) — it lives
    right next to this module in dashboard/ and is stdlib-only/self-contained, so it imports
    cleanly whether dispatcher.py is loaded by the control center server or by the standalone
    Windows-side worker.py."""
    import schedmath
    return schedmath


def approve_planner(jid: str, plan_yaml: str) -> dict:
    """Human gate: the operator approves (and may have edited) the planner's proposed plan.yaml.
    Validate the YAML, write it to the project root, ensure the registry points at it
    (plan_file:), and mark the job done. The planner itself never writes files — Atlas
    does, on approval (same shape as approve_architect)."""
    import yaml
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    if job.get("kind") != "planner":
        raise ValueError(f"job {jid} is not a planner job")

    # --- REESTIMATE mode (docs/scheduling.md §7): the approved output is a single revised
    # `remaining` for ONE task, captured by the worker onto job['proposal']['remaining']
    # (mirrors job['proposal']['plan_yaml']/['schedule.yaml'] for the other two modes). There is
    # no YAML to validate/write here — Atlas applies it through save_remainder (§6), the SAME
    # gate the manual remainder-review UI uses, so approving a reestimate auto-reconciles the
    # schedule exactly like a manual remainder edit would, and NEVER writes schedule.yaml directly.
    if job.get("mode") == "reestimate":
        # The operator may have EDITED the proposed number before approving (the same 'planYaml' textarea
        # the plan/schedule branches read below, reused here for the single `remaining` field —
        # the control center front-end's plannerApprove always posts it as `plan_yaml`). Honor the edit when
        # present and sane; fall back to the planner's own proposal only when it's blank/absent.
        prop = job.get("proposal") or {}
        edited = (plan_yaml or "").strip()
        if edited:
            if _dur_to_hours(edited) is None:
                raise ValueError(f"edited remaining {edited!r} is not a valid duration "
                                 "(e.g. '3h', '2h30', '90m', '0')")
            remaining = edited
        else:
            remaining = (prop.get("remaining") or "").strip()
        if not remaining:
            raise ValueError("planner proposed no remaining value to approve")
        task_id = job.get("task_id")
        if not task_id:
            raise ValueError(f"job {jid} has no task_id")
        res = save_remainder(job["project"], task_id, remaining=remaining, complete=False)
        job["proposal_written"] = True
        job["status"] = "done"
        job["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        _write(job)
        note = f"remaining set to {remaining!r} for {job['project']}/{task_id}"
        if res.get("reconcile"):
            note += "; schedule reconciled"
        return {"ok": True, "note": note}

    py_ = (plan_yaml or "").strip()
    if not py_:
        raise ValueError("plan.yaml is empty")
    try:
        if not isinstance(yaml.safe_load(py_), dict):
            raise ValueError("plan.yaml is not a mapping")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"plan.yaml is not valid YAML: {e}")

    # --- SCHEDULE mode (docs/scheduling.md §7): the approved YAML is Atlas's OWN schedule.yaml,
    # not a project sidecar. Atlas writes it (the planner never writes files), and there is no
    # registry/next_target step — schedule.yaml is global, and placements only REFERENCE tasks.
    if job.get("mode") == "schedule":
        parsed = yaml.safe_load(py_) or {}
        if not isinstance(parsed.get("placements", []), list):
            raise ValueError("schedule.yaml: `placements` must be a list")
        av = parsed.get("availability")
        if av is not None and not isinstance(av, dict):
            raise ValueError("schedule.yaml: `availability` must be a mapping")
        # Deterministic collision guard (Contract C1): reject the WHOLE proposal — never write
        # a partial schedule — if any two placements collide with each other or with life-events
        # already on disk (the proposal never carries events; see _preserve_events_on_write below),
        # or any placement lands on unavailable time (availability blocks/full_days/busy events).
        schedmath = _schedmath()
        check_sched = {
            "availability": av or {"blocks": [], "full_days": []},
            "placements": parsed.get("placements") or [],
            "events": _load_events_list(),          # the CURRENT on-disk events — what actually lands
        }
        seen_sessions = set()
        for i, pl in enumerate(check_sched["placements"]):
            if not isinstance(pl, dict):
                continue
            sess = pl.get("session")
            day, start, end = pl.get("day"), pl.get("start"), pl.get("end")
            if not sess or not day or not start or not end:
                continue   # incomplete entries can't be collision-checked; leave them to other validation
            if sess in seen_sessions:
                raise ValueError(f"schedule.yaml: duplicate session id {sess!r} in placements")
            seen_sessions.add(sess)
            reason = schedmath.collision(check_sched, str(day), str(start), str(end),
                                          exclude_session=sess)
            if reason:
                label = pl.get("task") or sess
                raise ValueError(f"schedule.yaml: {label!r} on {day} {start}–{end} {reason}")

        # Pinned respected by the planner (Contract C2): any on-disk placement the operator hand-edited
        # (`pinned: True`) survives a re-schedule untouched. Merge it into the written list
        # (dedup by session id — the pinned on-disk entry wins over a same-id proposal), and
        # drop any PROPOSED placement that collides with a preserved pinned one (pinned wins).
        pinned_existing = [p for p in _load_placements_list() if p.get("pinned")]
        pinned_sessions = {p.get("session") for p in pinned_existing}

        # A pinned placement wins over a same-slot PROPOSAL (below), but it must never survive
        # onto time the FINAL schedule marks unavailable — re-check each preserved pinned
        # placement against the proposal's own availability (blocks/full_days) and the on-disk
        # busy events (deliberately NOT against other placements — pinned intentionally wins
        # there, handled by the drop-on-collision loop below). Reject the whole write if a
        # pinned placement collides with unavailable time; nothing invalid is ever written.
        avail_only_sched = {"availability": check_sched["availability"], "placements": [],
                             "events": check_sched["events"]}
        for p in pinned_existing:
            if not isinstance(p, dict):
                continue
            sess, day, start, end = p.get("session"), p.get("day"), p.get("start"), p.get("end")
            if not sess or not day or not start or not end:
                continue
            reason = schedmath.collision(avail_only_sched, str(day), str(start), str(end))
            if reason:
                label = p.get("task") or sess
                raise ValueError(
                    f"schedule.yaml: pinned session {label!r} on {day} {start}–{end} {reason} "
                    "— pinned placements can't be preserved onto unavailable time"
                )

        pin_check_sched = {**check_sched, "placements": pinned_existing}
        final_placements = []
        for pl in check_sched["placements"]:
            if isinstance(pl, dict):
                if pl.get("session") in pinned_sessions:
                    continue   # a pinned on-disk entry replaces any same-id proposal
                day, start, end = pl.get("day"), pl.get("start"), pl.get("end")
                if day and start and end:
                    reason = schedmath.collision(pin_check_sched, str(day), str(start), str(end))
                    if reason:
                        continue   # collides with a pinned hand-edit — pinned wins, drop it
            final_placements.append(pl)
        final_placements.extend(pinned_existing)

        py_ = _preserve_events_on_write(py_)
        py_ = _splice_placements(py_, final_placements)
        SCHEDULE.write_text(py_, encoding="utf-8")
        job["status"], job["proposal_written"] = "done", True
        job["result"] = f"schedule.yaml written to {SCHEDULE}"
        job["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        _write(job)
        return job

    root = Path(_to_local(job["cwd"]))
    root.mkdir(parents=True, exist_ok=True)
    # PLAN mode is a whole-file overwrite (the planner authors the full plan.yaml), so the
    # ATLAS-WRITTEN task fields survive only if the planner happens to echo them back — which is
    # neither guaranteed nor enforceable. Re-assert them from disk first, the same guarantee the
    # SCHEDULE branch above gives pinned placements and life-events.
    py_, preserved, orphans = _preserve_atlas_task_fields(py_, root / "plan.yaml")
    (root / "plan.yaml").write_text(py_ + "\n", encoding="utf-8")
    plan_win_path = job["cwd"].rstrip("\\/").replace("/", "\\") + "\\plan.yaml"
    _ensure_plan_file_registry(job["project"], plan_win_path)
    note = f"plan.yaml written to {job['cwd']}"
    if preserved:
        note += f"; preserved Atlas fields on {preserved} task(s)"
    if orphans:
        # A task carrying a live dispatch link was dropped from the plan entirely. There is nothing
        # to preserve it onto — say so rather than losing it silently, since its merge can no longer
        # advance anything (docs/planner.md §6.6).
        note += "; dropped task(s) still linked to a job: " + ", ".join(sorted(orphans))
    # Refresh the DERIVED next_target (docs/planner.md §3: = title of the top `state: next` task).
    # The plan just changed the task set, so status.yaml must follow immediately — otherwise the
    # card shows a stale target until the next merge fires the merge-advance hook. Best-effort:
    # a status.yaml quirk must never fail the approval.
    try:
        import re as _re
        parsed = yaml.safe_load(py_) or {}
        nxt = None
        for t in (parsed.get("tasks") or []):
            if isinstance(t, dict) and t.get("state") == "next":
                nxt = t.get("title") or t.get("id")
                break
        sf = root / "status.yaml"
        if nxt and sf.exists():
            stext = sf.read_text(encoding="utf-8")
            v = '"' + str(nxt).replace("\\", "\\\\").replace('"', '\\"') + '"'
            newline = f"next_target: {v}"
            pat = _re.compile(r"^next_target:.*$", _re.MULTILINE)
            stext = pat.sub(lambda _m: newline, stext, count=1) if pat.search(stext) \
                else stext.rstrip("\n") + f"\n{newline}\n"
            sf.write_text(stext, encoding="utf-8")
            note += f"; next_target → {nxt}"
    except Exception:  # noqa: BLE001 — never fail approval on a status.yaml write quirk
        pass
    job["proposal"] = {"summary": (job.get("proposal") or {}).get("summary"), "plan_yaml": py_}
    job["proposal_written"] = True
    job["status"] = "done"
    job["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    _write(job)
    return {"ok": True, "note": note}


# ═══ the completion loop (docs/scheduling.md §6) — session-done → gated remainder review ═════════
# All completion state lives in the project's plan.yaml (the task ledger): `sessions_done` (which
# scheduled sessions are complete) + `remaining` (burn-down) + `review_pending` (a review is owed).
# schedule.yaml (the calendar) is NEVER mutated here. Edits are targeted text-surgery so the file's
# comments/formatting survive (stdlib + pyyaml only, same spirit as the worker's merge-advance hook).
_TASK_BLOCK_RE = re.compile(
    r"(?P<indent>[ \t]*)-\s*id:\s*\S.*?(?=\n[ \t]*-\s*id:|\n\S|\Z)", re.DOTALL)


def _task_field(block: str, key: str):
    m = re.search(rf"^\s*-?\s*{re.escape(key)}:\s*(.+?)\s*$", block, re.MULTILINE)
    if not m:
        return None
    v = m.group(1).strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def _set_task_scalar(block: str, key: str, value: str, indent: str) -> str:
    line = f"{indent}  {key}: {value}"
    pat = re.compile(rf"^{re.escape(indent)}  {re.escape(key)}:.*$", re.MULTILINE)
    return pat.sub(lambda _m: line, block, count=1) if pat.search(block) \
        else block.rstrip("\n") + f"\n{line}"


# ── whole-field text surgery, for fields that may not be scalars ─────────────────────────────────
# _task_field / _set_task_scalar assume a value that fits on the key's own line. `sessions_done` is
# a LIST, and while Atlas always writes it in flow style (`[sess_a, sess_b]`, see mark_session_done)
# plan.yaml is hand-editable, so a block-style value is legal and must round-trip:
#
#     sessions_done:
#       - sess_a
#
# _task_field cannot read that — its `\s*` after the colon crosses newlines, so it returns the FIRST
# ITEM ("- sess_a") and _set_task_scalar then writes `sessions_done: - sess_a`, which is not valid
# YAML. The merged-YAML guard catches it, but only to refuse the approval and blame an on-disk file
# that was perfectly valid. So preservation moves the field's WHOLE span instead of its scalar value.


def _task_field_span(block: str, key: str, indent: str):
    """(first, last) line indices of a task field's full text — its `key:` line plus any
    continuation lines indented deeper than it — or None when the key is absent."""
    lines = block.split("\n")
    head = f"{indent}  {key}:"
    for i, ln in enumerate(lines):
        if not (ln == head or ln.startswith(head + " ") or ln.startswith(head + "\t")):
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() \
                and (len(lines[j]) - len(lines[j].lstrip())) > len(indent) + 2:
            j += 1
        return i, j
    return None


def _task_field_raw(block: str, key: str, indent: str):
    """A task field's full raw text (key line + continuation lines), or None when absent."""
    span = _task_field_span(block, key, indent)
    return None if span is None else "\n".join(block.split("\n")[span[0]:span[1]])


def _put_task_field_raw(block: str, key: str, raw, indent: str) -> str:
    """Replace a task field's full span with `raw`, drop the field when `raw` is None, or append it
    when absent. The multi-line counterpart to _set_task_scalar."""
    span = _task_field_span(block, key, indent)
    if span is None:
        return block if raw is None else block.rstrip("\n") + "\n" + raw
    lines = block.split("\n")
    return "\n".join(lines[:span[0]] + ([] if raw is None else raw.split("\n")) + lines[span[1]:])


def _reindent(raw: str, old: str, new: str) -> str:
    """Re-indent a captured field span when the two files indent their task items differently."""
    if old == new or raw is None:
        return raw
    return "\n".join(new + ln[len(old):] if ln.startswith(old) else ln for ln in raw.split("\n"))


# ── Atlas-owned vs planner-owned task fields (the approve_planner preservation pass) ──────────────
# plan.yaml has TWO authors. The planner owns the backlog itself — id/title/milestone/type/priority/
# target_date/notes, the ordering, `state` (its whole job is maintaining it, and the operator hand-edits it
# too), and `estimate`/`remaining` (the charter tells it to estimate every task, and its reestimate
# mode exists precisely to revise `remaining`). Atlas owns the four fields below: pure machine
# bookkeeping the planner has no authority over and no way to know. It is told as much — the charter
# says it NEVER marks tasks done, "a deterministic hook does that on merge".
#
# Because a plan approval REPLACES the file wholesale, "the planner echoed it back" is the only thing
# that was keeping these alive. So disk is made the sole source of truth for them: present on disk →
# re-asserted onto the proposal; absent on disk → stripped from the proposal, so a planner cannot
# invent a `dispatch:` backlink pointing at a job it never launched (which would make a later merge
# advance the WRONG task). Same shape as _preserve_events_on_write: drop the proposal's version,
# inject the on-disk one.
_ATLAS_TASK_FIELDS = (
    "dispatch",        # the plan<->job backlink `_advance_plan_after_merge` matches on
    "sessions_done",   # which scheduled sessions are complete (mark_session_done)
    "review_pending",  # a remainder review is owed (merge hook / mark_session_done / save_remainder)
    "done_date",       # set when the operator confirms the task complete (save_remainder)
)
# Scope note: TASKS only. A milestone's `status`/`done_date` look similar but are genuinely
# planner-owned — sequencing milestones is its job, and `advance_milestone` is only a deterministic
# shortcut for the common case it would otherwise propose. These four are different in kind: no
# author but Atlas can know them.


def _plan_tasks_region(text: str):
    """(start, end) char offsets of the `tasks:` list body, or (None, None). Same boundary rule as
    _edit_plan_task: from the line after `tasks:` to the next column-0 key or EOF."""
    m = re.search(r"^tasks:\s*\n", text, re.MULTILINE)
    if not m:
        return None, None
    start = m.end()
    tail = re.search(r"\n\S", text[start:])
    return start, (start + tail.start() + 1 if tail else len(text))


def _preserve_atlas_task_fields(proposed_text: str, plan_path) -> tuple[str, int, list]:
    """Re-assert the Atlas-owned task fields from the on-disk plan.yaml onto an approved planner
    proposal, keyed by task id. Returns (text, n_tasks_touched, orphan_task_ids) where orphans are
    on-disk tasks carrying a `dispatch:` link that the proposal dropped entirely.

    Text surgery, not a YAML round-trip — the planner's own formatting and comments survive; only
    the four bookkeeping lines are rewritten. Best-effort by design: no on-disk plan (a project's
    first planner run), no `tasks:` section either side, or any read problem → the proposal is
    written exactly as approved, which is the pre-existing behaviour.
    """
    try:
        disk_text = Path(plan_path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 — no plan yet / unreadable → nothing to preserve
        return proposed_text, 0, []

    ds, de = _plan_tasks_region(disk_text)
    if ds is None:
        return proposed_text, 0, []
    on_disk = {}
    for bm in _TASK_BLOCK_RE.finditer(disk_text[ds:de]):
        blk = bm.group(0)
        tid = _task_field(blk, "id")
        if tid:
            dind = re.match(r"[ \t]*", blk).group(0)
            on_disk[tid] = (dind, {k: _task_field_raw(blk, k, dind) for k in _ATLAS_TASK_FIELDS})
    if not on_disk:
        return proposed_text, 0, []

    ps, pe = _plan_tasks_region(proposed_text)
    if ps is None:
        return proposed_text, 0, []
    tasks_text = proposed_text[ps:pe]
    blocks = list(_TASK_BLOCK_RE.finditer(tasks_text))
    if not blocks:
        # the proposal has a `tasks:` key but no recognizable `- id:` items (an empty plan, or a
        # shape this text surgery can't read). Nothing to merge onto, and NOT evidence that tasks
        # were dropped — stay silent rather than report every linked task as an orphan.
        return proposed_text, 0, []
    touched, seen = 0, set()
    # rebuild left-to-right: each block's replacement changes its length, so collect then join
    out, cursor = [], 0
    for bm in blocks:
        block = bm.group(0)
        tid = _task_field(block, "id")
        seen.add(tid)
        entry = on_disk.get(tid)
        if entry is not None:
            disk_indent, fields = entry
            indent = re.match(r"[ \t]*", block).group(0)
            changed = False
            for key in _ATLAS_TASK_FIELDS:
                before = block
                block = _put_task_field_raw(
                    block, key, _reindent(fields.get(key), disk_indent, indent), indent)
                changed = changed or block != before
            if changed:
                touched += 1
        out.append(tasks_text[cursor:bm.start()])
        out.append(block)
        cursor = bm.end()
    out.append(tasks_text[cursor:])
    merged = proposed_text[:ps] + "".join(out) + proposed_text[pe:]
    # The proposal was already validated as YAML; the values re-asserted here come from a file
    # nothing validates (plan.yaml is hand-editable). A malformed one on disk must fail loudly here
    # rather than write a broken plan — refusing the approval leaves the on-disk plan untouched.
    import yaml
    try:
        if not isinstance(yaml.safe_load(merged), dict):
            raise ValueError("not a mapping")
    except Exception as e:  # noqa: BLE001
        raise ValueError(
            f"preserving Atlas-written fields from {plan_path} produced invalid YAML ({e}) — "
            "the on-disk plan.yaml is likely malformed; fix it and approve again"
        )
    orphans = [tid for tid, (_, f) in on_disk.items() if f.get("dispatch") and tid not in seen]
    return merged, touched, orphans


def _plan_file_for(project_id: str):
    import yaml
    try:
        reg = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return None
    for e in (reg.get("projects") or []):
        if e.get("id") == project_id and e.get("plan_file"):
            return Path(_to_local(e["plan_file"]))
    return None


def _edit_plan_task(project_id: str, task_id: str, mutate) -> bool:
    """Apply `mutate(block, indent) -> block` to the plan.yaml task with id == task_id. Returns
    False (no crash) when the plan/file/task is missing."""
    pf = _plan_file_for(project_id)
    if not pf or not pf.exists():
        return False
    text = pf.read_text(encoding="utf-8")
    m = re.search(r"^tasks:\s*\n", text, re.MULTILINE)
    if not m:
        return False
    ts = m.end()
    tail = re.search(r"\n\S", text[ts:])
    te = ts + tail.start() + 1 if tail else len(text)
    tasks_text = text[ts:te]
    for bm in _TASK_BLOCK_RE.finditer(tasks_text):
        block = bm.group(0)
        if _task_field(block, "id") == task_id:
            indent = re.match(r"[ \t]*", block).group(0)
            nb = mutate(block, indent)
            tasks_text = tasks_text[:bm.start()] + nb + tasks_text[bm.end():]
            pf.write_text(text[:ts] + tasks_text + text[te:], encoding="utf-8")
            return True
    return False


def _refresh_next_target(project_id: str) -> None:
    """Re-derive status.yaml next_target = title of the top `state: next` task. Best-effort."""
    import yaml
    pf = _plan_file_for(project_id)
    if not pf or not pf.exists():
        return
    try:
        parsed = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
        nxt = next((t.get("title") or t.get("id") for t in (parsed.get("tasks") or [])
                    if isinstance(t, dict) and t.get("state") == "next"), None)
        sf = pf.parent / "status.yaml"
        if nxt and sf.exists():
            stext = sf.read_text(encoding="utf-8")
            v = '"' + str(nxt).replace("\\", "\\\\").replace('"', '\\"') + '"'
            pat = re.compile(r"^next_target:.*$", re.MULTILINE)
            stext = pat.sub(lambda _m: f"next_target: {v}", stext, count=1) if pat.search(stext) \
                else stext.rstrip("\n") + f"\nnext_target: {v}\n"
            sf.write_text(stext, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _sessions_done_list(block: str) -> list[str]:
    cur = _task_field(block, "sessions_done")
    if cur and cur.strip().startswith("["):
        return [x.strip() for x in cur.strip()[1:-1].split(",") if x.strip()]
    return []


def mark_session_done(project_id: str, task_id: str, session_id: str, done: bool = True) -> dict:
    """A scheduled session was completed (manual: the operator clicks it; dispatch: the merge hook). Record
    it on the task's `sessions_done`, move the task to `in_progress`, and flag `review_pending` so
    the remainder review surfaces. NEVER marks the TASK done — that is the operator's gate (§6)."""
    def mut(block, indent):
        ids = _sessions_done_list(block)
        if done and session_id and session_id not in ids:
            ids.append(session_id)
        elif not done and session_id in ids:
            ids.remove(session_id)
        block = _set_task_scalar(block, "sessions_done", "[" + ", ".join(ids) + "]", indent)
        if done:
            if _task_field(block, "state") in (None, "next", "todo"):
                block = _set_task_scalar(block, "state", "in_progress", indent)
            block = _set_task_scalar(block, "review_pending", "true", indent)
        return block
    ok = _edit_plan_task(project_id, task_id, mut)
    if not ok:
        raise ValueError(f"no plan task {project_id}/{task_id}")
    _refresh_next_target(project_id)
    return {"ok": True}


def advance_milestone(project_id: str) -> dict:
    """The cheap deterministic path for the common completion case: the ACTIVE milestone's tasks
    are all already `done` and the next milestone is already scoped, so closing it never needs
    the LLM planner. Re-verifies the guard itself (never trusts the client): every task tagged
    with the active milestone must be `done`, and a milestone with zero tasks is refused too
    (closing an unscoped milestone is meaningless). Promotes the first `next` milestone in FILE
    ORDER to `active` (none -> promotes nothing, not an error). Text surgery on the
    `milestones:` block only, mirroring `_edit_plan_task`'s `tasks:` surgery, so the rest of the
    file's comments/formatting survive untouched. `_TASK_BLOCK_RE`/`_task_field`/
    `_set_task_scalar` are generic over any `- id: ...` block, not just tasks, so they're reused
    here rather than duplicated."""
    import yaml
    pf = _plan_file_for(project_id)
    if not pf or not pf.exists():
        raise ValueError(f"no plan.yaml for {project_id}")
    text = pf.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(text) or {}
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not parse plan.yaml for {project_id}: {exc}") from exc

    milestones = [m for m in (parsed.get("milestones") or []) if isinstance(m, dict)]
    active = next((m for m in milestones if m.get("status") == "active"), None)
    if not active:
        raise ValueError(f"no active milestone for {project_id}")
    active_id = active.get("id")

    tasks = [t for t in (parsed.get("tasks") or []) if isinstance(t, dict)]
    own = [t for t in tasks if t.get("milestone") == active_id]
    if not own:
        raise ValueError(f"milestone {active_id} has no tasks — nothing to close")
    not_done = [t for t in own if t.get("state") != "done"]
    if not_done:
        raise ValueError(f"milestone {active_id} has {len(not_done)} task(s) not done")

    # first `next` milestone in FILE order (yaml.safe_load preserves list order from the file)
    promote = next((m for m in milestones if m.get("status") == "next"), None)
    promote_id = promote.get("id") if promote else None

    m = re.search(r"^milestones:\s*\n", text, re.MULTILINE)
    if not m:
        raise ValueError(f"plan.yaml for {project_id} has no milestones: block")
    ms = m.end()
    tail = re.search(r"\n\S", text[ms:])
    me = ms + tail.start() + 1 if tail else len(text)
    block_text = text[ms:me]

    today = dt.date.today().isoformat()
    edits = []   # (start, end, new_block), collected against the ORIGINAL block_text offsets
    for bm in _TASK_BLOCK_RE.finditer(block_text):
        block = bm.group(0)
        mid = _task_field(block, "id")
        indent = re.match(r"[ \t]*", block).group(0)
        if mid == active_id:
            nb = _set_task_scalar(block, "status", "done", indent)
            nb = _set_task_scalar(nb, "done_date", today, indent)
            edits.append((bm.start(), bm.end(), nb))
        elif promote_id and mid == promote_id:
            nb = _set_task_scalar(block, "status", "active", indent)
            edits.append((bm.start(), bm.end(), nb))
    if not edits:
        raise ValueError(f"could not locate milestone {active_id} in plan.yaml text")
    new_block_text = block_text
    for start, end, nb in reversed(edits):   # back-to-front so earlier offsets stay valid
        new_block_text = new_block_text[:start] + nb + new_block_text[end:]
    pf.write_text(text[:ms] + new_block_text + text[me:], encoding="utf-8")

    _refresh_next_target(project_id)
    return {
        "ok": True,
        "completed": active_id,
        "completed_title": active.get("title"),
        "promoted": promote_id,
        "promoted_title": promote.get("title") if promote else None,
    }


def _prune_future_placements(project_id: str, task_id: str, keep_sessions: list[str]) -> int:
    """When a task is completed, remove its still-scheduled (NOT-done) sessions from schedule.yaml —
    the work is finished, so that time is free again. Done sessions (in keep_sessions) STAY as
    history. Text surgery so the file's comments + availability survive. Returns how many were removed."""
    if not SCHEDULE.exists():
        return 0
    text = SCHEDULE.read_text(encoding="utf-8")
    m = re.search(r"^placements:\s*\n", text, re.MULTILINE)
    if not m:
        return 0
    ps = m.end()
    tail = re.search(r"\n\S", text[ps:])
    pe = ps + tail.start() + 1 if tail else len(text)
    body = text[ps:pe]
    keep = set(keep_sessions or [])
    item_re = re.compile(r"(?P<indent>[ \t]*)-\s.*?(?=\n[ \t]*-\s|\n\S|\Z)", re.DOTALL)
    spans = []
    for it in item_re.finditer(body):
        b = it.group(0)
        gp = lambda k: (re.search(rf"{k}:\s*([^\s,}}]+)", b) or [None, None])[1]
        if gp("project") == project_id and gp("task") == task_id and gp("session") not in keep:
            end = it.end() + 1 if it.end() < len(body) and body[it.end()] == "\n" else it.end()
            spans.append((it.start(), end))
    for a, b in reversed(spans):     # remove back-to-front so offsets stay valid
        body = body[:a] + body[b:]
    if spans:
        SCHEDULE.write_text(text[:ps] + body + text[pe:], encoding="utf-8")
    return len(spans)


def save_remainder(project_id: str, task_id: str, remaining: str = "", complete: bool = False) -> dict:
    """The operator's GATE on the remainder review (§6). Either set the task's revised `remaining` (task
    stays in_progress) OR mark the whole task done (remaining 0 + done_date). Clears review_pending.
    Completing a task also PRUNES its still-scheduled sessions from the calendar (the work is done)."""
    today = dt.date.today().isoformat()

    def mut(block, indent):
        if complete:
            block = _set_task_scalar(block, "remaining", "0", indent)
            block = _set_task_scalar(block, "state", "done", indent)
            block = _set_task_scalar(block, "done_date", today, indent)
        else:
            rem = (remaining or "").strip() or "0"
            block = _set_task_scalar(block, "remaining", rem, indent)
            block = _set_task_scalar(block, "state", "in_progress", indent)
        block = _set_task_scalar(block, "review_pending", "false", indent)
        return block
    ok = _edit_plan_task(project_id, task_id, mut)
    if not ok:
        raise ValueError(f"no plan task {project_id}/{task_id}")
    pruned = 0
    reconciled = None
    if complete:
        # keep the DONE sessions (history), drop the rest from the calendar
        import yaml
        keep = []
        pf = _plan_file_for(project_id)
        try:
            parsed = yaml.safe_load(pf.read_text(encoding="utf-8")) or {} if pf else {}
            for t in (parsed.get("tasks") or []):
                if t.get("id") == task_id:
                    keep = [str(x) for x in (t.get("sessions_done") or [])]
        except Exception:  # noqa: BLE001
            pass
        pruned = _prune_future_placements(project_id, task_id, keep)
    else:
        # auto-trigger (docs/scheduling.md): a raised `remaining` may now exceed what's already
        # on the calendar — close that gap deterministically (no-op if it doesn't).
        try:
            reconciled = reconcile_task(project_id, task_id)
        except ValueError:  # never let a reconciliation quirk fail the gated remainder save itself
            reconciled = None
    _refresh_next_target(project_id)
    out = {"ok": True, "pruned": pruned}
    if reconciled is not None:
        out["reconcile"] = reconciled
    return out


# ═══ life-events — Part 2 (docs/scheduling-part2.md §8) ══════════════════════════════════════
# The operator's own typed calendar items, captured from the desktop. Structured-splice writes: parse +
# re-serialize ONLY the `events:` list, then splice it back in place of the old `events:` region —
# every other section of schedule.yaml (header comments, availability, placements) is left
# byte-for-byte intact. The scheduling assistant never touches this section (see approve_planner).
_EVENT_TYPES = {"appointment", "errand", "personal", "free"}
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _events_region(text: str):
    """(start, end) char offsets of the `events:` block — from a line matching ^events: to the
    next top-level key or EOF. (None, None) if the file has no `events:` key yet.
    NOTE: the boundary must match an actual `key:` at column 0, NOT just any non-whitespace
    char — yaml.safe_dump writes block-sequence items at column 0 too (`- id: ...`), which would
    otherwise be mistaken for the next top-level key and truncate the region mid-list.
    The `events:` line itself is matched with a trailing `.*` so BOTH the block form (`events:\\n`)
    AND the inline-empty form (`events: []`) are found — otherwise a first save onto an empty
    `events: []` file finds no region and APPENDS a duplicate `events:` key (malformed YAML)."""
    m = re.search(r"^events:.*\n", text, re.MULTILINE)
    if not m:
        return None, None
    tail = re.search(r"\n[A-Za-z_][\w.-]*:", text[m.end():])
    end = m.end() + tail.start() + 1 if tail else len(text)
    return m.start(), end


def _load_events_list() -> list:
    import yaml
    if not SCHEDULE.exists():
        return []
    try:
        data = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — a broken schedule.yaml never crashes a save/delete
        return []
    return [e for e in (data.get("events") or []) if isinstance(e, dict)]


def _write_events_list(events: list) -> None:
    """Re-serialize + splice `events` back into schedule.yaml, preserving everything else."""
    import yaml
    block = yaml.safe_dump({"events": events}, sort_keys=False, allow_unicode=True)
    if SCHEDULE.exists():
        text = SCHEDULE.read_text(encoding="utf-8")
    else:                              # defensive: create a minimal file rather than raise
        text = "availability:\n  blocks: []\n  full_days: []\n\nplacements: []\n"
    start, end = _events_region(text)
    new_text = (text.rstrip("\n") + "\n\n" + block) if start is None else (text[:start] + block + text[end:])
    SCHEDULE.write_text(new_text if new_text.endswith("\n") else new_text + "\n", encoding="utf-8")


def _preserve_events_on_write(proposed_text: str) -> str:
    """Before writing an APPROVED schedule.yaml proposal (SCHEDULE mode): strip any `events:`
    section the scheduling assistant emitted and re-inject the CURRENT on-disk events instead.
    The assistant proposes availability/placements only — life-events are the operator's, captured via
    save_event/delete_event, and must never be invented or clobbered by an approval
    (docs/scheduling-part2.md §8)."""
    import yaml
    current_events = _load_events_list()          # reads the file as it stands BEFORE this write
    start, end = _events_region(proposed_text)
    trimmed = proposed_text if start is None else (proposed_text[:start] + proposed_text[end:])
    block = yaml.safe_dump({"events": current_events}, sort_keys=False, allow_unicode=True)
    return trimmed.rstrip("\n") + "\n\n" + block.rstrip("\n")


def _clean_event(event: dict) -> dict:
    """Validate + allowlist one life-event's fields (docs/scheduling-part2.md §8). Raises
    ValueError on invalid input (bad/missing title or day) so the endpoint can 400."""
    title = str(event.get("title") or "").strip()
    if not title:
        raise ValueError("event requires a title")
    day = str(event.get("day") or "").strip()
    try:
        dt.date.fromisoformat(day)
    except (ValueError, TypeError):
        raise ValueError(f"event requires a valid day (YYYY-MM-DD): {day!r}")
    etype = str(event.get("type") or "personal").strip().lower()
    if etype not in _EVENT_TYPES:
        etype = "personal"
    eid = str(event.get("id") or "").strip() or f"evt_{uuid.uuid4().hex[:8]}"
    out = {"id": eid, "title": title, "day": day, "type": etype}
    start, end = event.get("start"), event.get("end")
    if start and end and _HHMM_RE.match(str(start)) and _HHMM_RE.match(str(end)):
        out["start"], out["end"] = str(start), str(end)
    if "busy" in event and event.get("busy") is not None:
        out["busy"] = bool(event["busy"])
    for k in ("remind", "location", "notes"):
        v = event.get(k)
        if v not in (None, ""):
            out[k] = v
    recur = event.get("recur")
    if isinstance(recur, dict) and recur:
        out["recur"] = recur
    return out


def save_event(event: dict) -> dict:
    """Upsert one life-event into schedule.yaml's `events:` list, by `id` (generated if absent
    or falsy). Returns the saved (cleaned) event, including its id.

    Deterministic collision guard (Contract C1): a busy, timed event that collides with any
    existing placement/event/unavailable-block on its day is REFUSED — raises ValueError with a
    human-readable reason, and nothing is written. (An event explicitly marked `busy: false` is
    never a collision candidate.) This also closes the prior gap where the same slot could be
    double-booked by saving it twice.

    A busy, ALL-DAY event (no start/end) occupies the whole 06:00-23:00 day — landing it on a
    day that already has ANY busy item (a placement, a timed event, or another all-day event)
    is REFUSED the same way (docs/scheduling-part2.md: all-day = hard block, not "day is free
    after hours")."""
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    cleaned = _clean_event(event)
    is_timed = bool(cleaned.get("start") and cleaned.get("end"))
    if cleaned.get("busy", True):
        import yaml
        schedmath = _schedmath()
        try:
            sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
                if SCHEDULE.exists() else {}
        except Exception:  # noqa: BLE001 — a broken schedule.yaml must never block on THIS write
            sched = {}
        if is_timed:
            reason = schedmath.collision(sched, cleaned["day"], cleaned["start"], cleaned["end"],
                                          exclude_event_id=cleaned["id"])
        else:
            reason = schedmath.day_busy(sched, cleaned["day"], exclude_event_id=cleaned["id"])
        if reason:
            raise ValueError(f"can't save {cleaned['title']!r}: {reason}")
    events = _load_events_list()
    for i, e in enumerate(events):
        if e.get("id") == cleaned["id"]:
            events[i] = cleaned
            break
    else:
        events.append(cleaned)
    _write_events_list(events)
    return cleaned


def delete_event(event_id: str) -> dict:
    """Remove one life-event by id from schedule.yaml. No-op if the id isn't present."""
    eid = str(event_id or "").strip()
    if not eid:
        raise ValueError("event id required")
    events = [e for e in _load_events_list() if e.get("id") != eid]
    _write_events_list(events)
    return {"ok": True}


def move_event(event_id: str, day: str, start: str | None = None, end: str | None = None) -> dict:
    """Contract C2: retime one life-event (day-view drag). A missing/blank `start`/`end` leaves
    that field as it was (e.g. dragging an all-day event to a new day). Collision-guarded when
    the event ends up busy + timed (against overlapping busy items on the target day), OR busy +
    all-day (against ANY busy item on the target day — an all-day event is a hard block on the
    whole day, docs/scheduling-part2.md). A `busy: false` event can move freely.

    NOTE (recurring events): if the event has a `recur`, patching `day` shifts the whole series
    anchor — every occurrence moves with it. That's the accepted, predictable behavior here; this
    function never splits a series. Whether to allow that drag at all (vs. day-only for singles)
    is a frontend (C3) call."""
    eid = str(event_id or "").strip()
    if not eid:
        raise ValueError("event id required")
    day = str(day or "").strip()
    try:
        dt.date.fromisoformat(day)
    except (ValueError, TypeError):
        raise ValueError(f"invalid day: {day!r}")
    events = _load_events_list()
    idx = next((i for i, e in enumerate(events) if e.get("id") == eid), None)
    if idx is None:
        raise ValueError(f"unknown event: {eid!r}")
    ev = dict(events[idx])
    start_s = str(start).strip() if start else ev.get("start")
    end_s = str(end).strip() if end else ev.get("end")
    busy = bool(ev.get("busy")) if "busy" in ev else True
    if busy:
        import yaml
        schedmath = _schedmath()
        try:
            sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
                if SCHEDULE.exists() else {}
        except Exception:  # noqa: BLE001 — a broken schedule.yaml must never block on THIS write
            sched = {}
        if start_s and end_s:
            reason = schedmath.collision(sched, day, start_s, end_s, exclude_event_id=eid)
        else:
            reason = schedmath.day_busy(sched, day, exclude_event_id=eid)
        if reason:
            raise ValueError(f"can't move {ev.get('title') or eid!r}: {reason}")
    ev["day"] = day
    if start_s:
        ev["start"] = start_s
    if end_s:
        ev["end"] = end_s
    events[idx] = ev
    _write_events_list(events)
    return ev


def override_occurrence(event_id: str, date: str, start: str, end: str) -> dict:
    """Per-occurrence exception (docs/scheduling-part2.md §8b): retime a SINGLE occurrence of a
    recurring life-event, leaving the rest of the series untouched. Writes
    `overrides: {date: {start, end}}` on the event (schedmath/control center-state both honor it — the
    retimed date renders/collides at the NEW times, every other date keeps the series' base
    timing). Raises ValueError (and writes nothing) if the event id is unknown, isn't recurring
    (no `recur` block), the date/times are malformed, or the new slot collides with anything else
    on `date` — the collision check excludes this event's own id, so it never collides with its
    own (about-to-be-overridden) occurrence."""
    eid = str(event_id or "").strip()
    if not eid:
        raise ValueError("event id required")
    date_s = str(date or "").strip()
    try:
        dt.date.fromisoformat(date_s)
    except (ValueError, TypeError):
        raise ValueError(f"invalid date: {date_s!r}")
    start_s, end_s = str(start or "").strip(), str(end or "").strip()
    if not (_HHMM_RE.match(start_s) and _HHMM_RE.match(end_s)):
        raise ValueError(f"invalid start/end: {start!r}/{end!r}")
    events = _load_events_list()
    idx = next((i for i, e in enumerate(events) if e.get("id") == eid), None)
    if idx is None:
        raise ValueError(f"unknown event: {eid!r}")
    ev = dict(events[idx])
    if not (isinstance(ev.get("recur"), dict) and ev["recur"]):
        raise ValueError(f"{eid!r} is not a recurring event")
    import yaml
    schedmath = _schedmath()
    try:
        sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
            if SCHEDULE.exists() else {}
    except Exception:  # noqa: BLE001 — a broken schedule.yaml must never block on THIS write
        sched = {}
    reason = schedmath.collision(sched, date_s, start_s, end_s, exclude_event_id=eid)
    if reason:
        raise ValueError(f"can't retime {ev.get('title') or eid!r} on {date_s}: {reason}")
    overrides = dict(ev.get("overrides") or {})
    overrides[date_s] = {"start": start_s, "end": end_s}
    ev["overrides"] = overrides
    events[idx] = ev
    _write_events_list(events)
    return ev


def skip_occurrence(event_id: str, date: str) -> dict:
    """Per-occurrence exception (docs/scheduling-part2.md §8b): delete a SINGLE occurrence of a
    recurring life-event, leaving the rest of the series untouched. Appends `date` to the event's
    `skips` list (deduped) and clears any existing `overrides[date]` (a skipped occurrence has no
    timing to retime). Raises ValueError (and writes nothing) if the event id is unknown, isn't
    recurring (no `recur` block), or the date is malformed."""
    eid = str(event_id or "").strip()
    if not eid:
        raise ValueError("event id required")
    date_s = str(date or "").strip()
    try:
        dt.date.fromisoformat(date_s)
    except (ValueError, TypeError):
        raise ValueError(f"invalid date: {date_s!r}")
    events = _load_events_list()
    idx = next((i for i, e in enumerate(events) if e.get("id") == eid), None)
    if idx is None:
        raise ValueError(f"unknown event: {eid!r}")
    ev = dict(events[idx])
    if not (isinstance(ev.get("recur"), dict) and ev["recur"]):
        raise ValueError(f"{eid!r} is not a recurring event")
    skips = list(ev.get("skips") or [])
    if date_s not in skips:
        skips.append(date_s)
    ev["skips"] = skips
    overrides = dict(ev.get("overrides") or {})
    if overrides.pop(date_s, None) is not None:
        if overrides:
            ev["overrides"] = overrides
        else:
            ev.pop("overrides", None)
    events[idx] = ev
    _write_events_list(events)
    return ev


# ═══ placements — hand-edit move/delete (Contract C2) ══════════════════════════════════════
# Same structured-splice idiom as the events section above: parse + re-serialize ONLY the
# `placements:` list, then splice it back in place of the old `placements:` region — comments,
# availability, and events are left byte-for-byte intact. A hand-edit (drag/resize/delete) is
# re-validated against the collision guard server-side and marks the item `pinned: True` so the
# LLM scheduler fits new work around it rather than silently reshuffling the operator's own edit.

def _placements_region(text: str):
    """(start, end) char offsets of the `placements:` block — same idiom as `_events_region`,
    but ALSO backs the boundary up over any comment/blank lines leading into the next top-level
    section. Unlike `events:` (always the last section today, so it never bumps into this), the
    real schedule.yaml has an explanatory `# EVENTS ...` comment directly above `events:` —
    naively stopping right at that key would swallow the comment into the placements splice and
    lose it on every hand-edit. Walking back over that preamble keeps it where it belongs."""
    m = re.search(r"^placements:.*\n", text, re.MULTILINE)
    if not m:
        return None, None
    body_start = m.end()
    key_m = re.search(r"^[A-Za-z_][\w.-]*:", text[body_start:], re.MULTILINE)
    if key_m is None:
        return m.start(), len(text)
    end = body_start + key_m.start()
    preamble_lines = text[body_start:end].splitlines(keepends=True)
    while preamble_lines and re.match(r"^[ \t]*(#.*)?\n?$", preamble_lines[-1]):
        end -= len(preamble_lines.pop())
    return m.start(), end


def _splice_placements(text: str, placements: list) -> str:
    """Return `text` with its `placements:` region replaced by the (re-serialized) `placements`
    list — everything else in `text` is left untouched."""
    import yaml
    block = yaml.safe_dump({"placements": placements}, sort_keys=False, allow_unicode=True)
    start, end = _placements_region(text)
    new_text = (text.rstrip("\n") + "\n\n" + block) if start is None else (text[:start] + block + text[end:])
    return new_text if new_text.endswith("\n") else new_text + "\n"


def _load_placements_list() -> list:
    import yaml
    if not SCHEDULE.exists():
        return []
    try:
        data = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — a broken schedule.yaml never crashes a hand-edit
        return []
    return [p for p in (data.get("placements") or []) if isinstance(p, dict)]


def _write_placements_list(placements: list) -> None:
    """Re-serialize + splice `placements` back into schedule.yaml, preserving everything else
    (availability, comments, events) — same idiom as `_write_events_list`."""
    if SCHEDULE.exists():
        text = SCHEDULE.read_text(encoding="utf-8")
    else:                              # defensive: create a minimal file rather than raise
        text = "availability:\n  blocks: []\n  full_days: []\n\nevents: []\n"
    SCHEDULE.write_text(_splice_placements(text, placements), encoding="utf-8")


def move_placement(session: str, day: str, start: str, end: str) -> dict:
    """Contract C2: reposition/resize one placement (day-view drag or resize — resize is just a
    different `end`). Re-validated against the collision guard before writing; an invalid target
    raises ValueError and the file is left untouched. Marks the placement `pinned: True` so the
    scheduling assistant fits new work around this hand-edit rather than silently reshuffling it."""
    sess = str(session or "").strip()
    if not sess:
        raise ValueError("session required")
    day = str(day or "").strip()
    try:
        dt.date.fromisoformat(day)
    except (ValueError, TypeError):
        raise ValueError(f"invalid day: {day!r}")
    placements = _load_placements_list()
    idx = next((i for i, p in enumerate(placements) if p.get("session") == sess), None)
    if idx is None:
        raise ValueError(f"unknown placement: {sess!r}")
    import yaml
    schedmath = _schedmath()
    try:
        sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
            if SCHEDULE.exists() else {}
    except Exception:  # noqa: BLE001 — a broken schedule.yaml must never block on THIS write
        sched = {}
    reason = schedmath.collision(sched, day, start, end, exclude_session=sess)
    if reason:
        label = placements[idx].get("task") or sess
        raise ValueError(f"can't move {label!r}: {reason}")
    pl = dict(placements[idx])
    pl["day"], pl["start"], pl["end"], pl["pinned"] = day, str(start), str(end), True
    placements[idx] = pl
    _write_placements_list(placements)
    return pl


def delete_placement(session: str) -> dict:
    """Contract C2: remove one placement (day-view delete). Does NOT touch plan.yaml — the
    task's `sessions:` entry is left as-is; it simply reads as unscheduled again (the C5
    "unplanned" line already surfaces that)."""
    sess = str(session or "").strip()
    if not sess:
        raise ValueError("session required")
    placements = _load_placements_list()
    remaining = [p for p in placements if p.get("session") != sess]
    if len(remaining) == len(placements):
        raise ValueError(f"unknown placement: {sess!r}")
    _write_placements_list(remaining)
    return {"ok": True}


def _new_session_id(existing: set) -> str:
    """A fresh 'sess_<8 hex>' id guaranteed not to collide with any id already in `existing`."""
    while True:
        sid = "sess_" + secrets.token_hex(4)
        if sid not in existing:
            return sid


def add_placement(project: str, task: str, day: str, start: str, end: str) -> dict:
    """Insert a brand-new pinned placement — a scheduled slice of a task with no session yet.
    Backs the plan↔schedule reconciliation's split-fill (`reconcile_task`) and any direct "add a
    session" UI action. Collision-guarded like `move_placement`: a colliding target raises
    ValueError and the file is left untouched. The fresh session id is generated here and
    guaranteed unique against every placement already on the calendar."""
    project = str(project or "").strip()
    task = str(task or "").strip()
    if not project or not task:
        raise ValueError("project and task required")
    day = str(day or "").strip()
    try:
        dt.date.fromisoformat(day)
    except (ValueError, TypeError):
        raise ValueError(f"invalid day: {day!r}")
    import yaml
    schedmath = _schedmath()
    try:
        sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
            if SCHEDULE.exists() else {}
    except Exception:  # noqa: BLE001 — a broken schedule.yaml must never block on THIS write
        sched = {}
    reason = schedmath.collision(sched, day, start, end)
    if reason:
        raise ValueError(f"can't add {task!r}: {reason}")
    placements = _load_placements_list()
    existing = {p.get("session") for p in placements if p.get("session")}
    session = _new_session_id(existing)
    pl = {"session": session, "project": project, "task": task,
          "day": day, "start": str(start), "end": str(end), "pinned": True}
    placements.append(pl)
    _write_placements_list(placements)
    return pl


def _hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _dur_to_hours(v) -> float | None:
    """A task's effort duration -> float hours. Mirrors the control center's `_dur_to_hours`
    (kept as a small private duplicate here rather than an import — dispatcher.py is never
    allowed to depend on the control center package, see `_schedmath()`'s note). Accepts '3h' | '2h30' |
    '45m' | '1.5h' | 2 | 0; None when absent/unparseable."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)h(?:\s*(\d{1,2}))?$", s)
    if m:
        return float(m.group(1)) + (int(m.group(2)) / 60.0 if m.group(2) else 0.0)
    m = re.match(r"^(\d+(?:\.\d+)?)m$", s)
    if m:
        return float(m.group(1)) / 60.0
    m = re.match(r"^\d+(?:\.\d+)?$", s)
    if m:
        return float(s)
    return None


def _task_hours(project_id: str, task_id: str):
    """(remaining_h, scheduled_future_h) for one plan task, read directly from plan.yaml +
    schedule.yaml — the same numbers the control center's `_annotate_task_sessions` derives for the
    UI, recomputed independently here since dispatcher.py never imports the control center package.
    `remaining_h` is None when the task isn't estimated (nothing to reconcile against). None
    (the whole tuple) when the plan/task itself is missing."""
    import yaml
    pf = _plan_file_for(project_id)
    if not pf or not pf.exists():
        return None
    try:
        parsed = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return None
    task = next((t for t in (parsed.get("tasks") or [])
                 if isinstance(t, dict) and t.get("id") == task_id), None)
    if task is None:
        return None
    est_h = _dur_to_hours(task.get("estimate"))
    rem_h = _dur_to_hours(task.get("remaining"))
    if rem_h is None and est_h is not None:
        rem_h = 0.0 if task.get("state") == "done" else est_h
    done_ids = {str(x) for x in (task.get("sessions_done") or []) if x}
    try:
        sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
            if SCHEDULE.exists() else {}
    except Exception:  # noqa: BLE001
        sched = {}
    schedmath = _schedmath()
    future_min = 0
    for pl in (sched.get("placements") or []):
        if not isinstance(pl, dict) or pl.get("project") != project_id or pl.get("task") != task_id:
            continue
        if pl.get("session") in done_ids:
            continue
        try:
            s0, e0 = schedmath.to_min(str(pl.get("start"))), schedmath.to_min(str(pl.get("end")))
        except ValueError:
            continue
        if e0 > s0:
            future_min += e0 - s0
    return rem_h, future_min / 60.0


def reconcile_task(project_id: str, task_id: str) -> dict:
    """Plan<->schedule reconciliation (docs/scheduling.md): after a task's `remaining` grows past
    what's already on the calendar, close the gap deterministically — grow-then-split-fill.

    1. GROW IN PLACE: the task's latest not-done placement is extended — its END later first,
       then (if still short) its START earlier — as far as it fits without a collision and within
       06:00-23:00. Written via `move_placement` (keeps `pinned: True`).
    2. SPLIT-FILL THE REST: any leftover gap is carved into fresh placements via
       `schedmath.fill_free_slots`, each written with `add_placement`.

    No-op (all-zero summary) when the task is already fully scheduled or unestimated. Returns
    `{"grew": <minutes>, "added": [<slice>, ...], "unfilled_min": <n>}` — `unfilled_min > 0` means
    the horizon ran out before the gap closed (surfaced, never silently dropped). Never places on
    unavailable/occupied time; deterministic."""
    hours = _task_hours(project_id, task_id)
    if hours is None:
        raise ValueError(f"no plan task {project_id}/{task_id}")
    remaining_h, scheduled_h = hours
    if remaining_h is None:
        return {"grew": 0, "added": [], "unfilled_min": 0}
    gap_min = round((remaining_h - scheduled_h) * 60)
    if gap_min <= 0:
        return {"grew": 0, "added": [], "unfilled_min": 0}

    import yaml
    schedmath = _schedmath()
    try:
        sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
            if SCHEDULE.exists() else {}
    except Exception:  # noqa: BLE001
        sched = {}
    pf = _plan_file_for(project_id)
    done_ids: set = set()
    try:
        parsed = yaml.safe_load(pf.read_text(encoding="utf-8")) or {} if pf and pf.exists() else {}
        for t in (parsed.get("tasks") or []):
            if t.get("id") == task_id:
                done_ids = {str(x) for x in (t.get("sessions_done") or []) if x}
    except Exception:  # noqa: BLE001
        pass

    candidates = [p for p in (sched.get("placements") or [])
                  if isinstance(p, dict) and p.get("project") == project_id
                  and p.get("task") == task_id and p.get("session") not in done_ids]
    candidates.sort(key=lambda p: (str(p.get("day")), str(p.get("start"))))
    latest = candidates[-1] if candidates else None

    grew_min = 0
    from_day = dt.date.today().isoformat()
    if latest is not None:
        sess = latest.get("session")
        day = str(latest.get("day"))
        from_day = day
        try:
            s0 = schedmath.to_min(str(latest.get("start")))
            e0 = schedmath.to_min(str(latest.get("end")))
        except ValueError:
            s0 = e0 = None
        if s0 is not None and e0 is not None:
            busy = sorted(schedmath.busy_intervals(sched, day, exclude_session=sess),
                           key=lambda t: t[0])
            right_bound = schedmath.DAY_END_MIN
            for b0, _b1, _label in busy:
                if b0 >= e0:
                    right_bound = min(right_bound, b0)
            left_bound = schedmath.DAY_START_MIN
            for _b0, b1, _label in busy:
                if b1 <= s0:
                    left_bound = max(left_bound, b1)
            need = gap_min
            new_e = min(right_bound, e0 + need)
            need -= max(0, new_e - e0)
            new_s = max(left_bound, s0 - need)
            if new_e > e0 or new_s < s0:
                reason = schedmath.collision(sched, day, _hhmm(new_s), _hhmm(new_e),
                                              exclude_session=sess)
                if reason is None:
                    move_placement(sess, day, _hhmm(new_s), _hhmm(new_e))
                    grew_min = (new_e - e0) + (s0 - new_s)
                    try:
                        sched = yaml.safe_load(SCHEDULE.read_text(encoding="utf-8")) or {} \
                            if SCHEDULE.exists() else {}
                    except Exception:  # noqa: BLE001
                        sched = {}

    left = gap_min - grew_min
    added: list[dict] = []
    unfilled_min = 0
    if left > 0:
        slots = schedmath.fill_free_slots(sched, from_day, left)
        filled = 0
        for slot in slots:
            pl = add_placement(project_id, task_id, slot["day"], slot["start"], slot["end"])
            added.append({"session": pl.get("session"), "day": pl.get("day"),
                          "start": pl.get("start"), "end": pl.get("end")})
            filled += schedmath.to_min(slot["end"]) - schedmath.to_min(slot["start"])
        unfilled_min = max(0, left - filled)
    return {"grew": grew_min, "added": added, "unfilled_min": unfilled_min}


def answer_job(jid: str, answer: str) -> dict:
    """Record the operator's answer to a needs_input job and re-queue it for a resume run.
    The worker picks it up, runs `claude --resume <session_id>` in the same sandbox,
    and either finishes (done) or asks again (needs_input)."""
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    if job.get("status") != "needs_input":
        raise ValueError(f"job {jid} is {job.get('status')!r}, not awaiting an answer")
    # A FANNED job has no session of its own — each lane owns one. The answer routes to the
    # lane that asked (`needs_input_lane`), so validate against that lane's session instead.
    if not job.get("session_id"):
        lanes = job.get("lanes") or []
        target = job.get("needs_input_lane")
        resumable = next((x for x in lanes
                          if x.get("lane_id") == target and x.get("session_id")), None)
        if not resumable:
            raise ValueError(f"job {jid} has no session_id to resume")
    job["answer"] = answer
    job["resume"] = True
    job["status"] = "queued"
    _write(job)
    return job


def confirm_contract(jid: str, contract: dict) -> dict:
    """Human gate #1: the operator confirms (and may have edited) the contract a scoping session
    produced. Denormalize it onto the job and re-queue as a fresh **exec** (coding) job.

    The scoping session is finished; coding runs as a new session, so we clear the resume
    state. (Pre-Stage-4 the coding run is the existing single pass → 'done'; Stage 4 will
    chain it into testing → review → ready.)"""
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    if not job.get("contract"):
        raise ValueError(f"job {jid} has no contract to confirm")
    # MERGE, don't replace: the confirm form only round-trips the fields the operator can edit
    # (brief / acceptance_criteria / test_cmd). Replacing wholesale silently drops every other
    # contract key the scoper produced — the `intent` the operator approved on, and the multi-worker
    # `plan` that decides whether this job fans out at all.
    merged = dict(job.get("contract") or {})
    merged.update({k: v for k, v in (contract or {}).items() if v is not None})
    job["contract"] = merged
    contract = merged
    job["contract_confirmed"] = True
    job["acceptance_criteria"] = contract.get("acceptance_criteria")
    if contract.get("test_cmd"):
        job["test_cmd"] = contract["test_cmd"]
    job["task"] = (contract.get("brief") or job.get("task") or "").strip()
    job["kind"] = "exec"
    job["scope"] = False
    job["permission_mode"] = "skip"        # coding is autonomous (sandbox-contained)
    job["status"] = "queued"
    job["session_id"] = None               # coding is a fresh session, not a resume
    job["resume"] = False
    job["answer"] = None
    job["question"] = None
    _write(job)
    return job


def mark_merged(jid: str) -> dict:
    """Record-keeping for human gate #2: the operator merged the `atlas/<id>` branch himself (Atlas
    never auto-merges into a live branch). Flips a `ready` job to `merged`; the worktree can
    then be discarded. Does not run git."""
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    job["status"] = "merged"
    _write(job)
    return job


def request_merge(jid: str) -> dict:
    """Human gate #2, ONE CLICK: flag a `ready` job for the worker to ACTUALLY merge its
    `atlas/<id>` branch into the project's base branch, then bump status.yaml + clean up. The
    worker (owns git) does the merge and, on conflict, aborts + routes back to ready with a note.
    Still human-gated — the operator clicks it — it just removes the manual command + the mark step."""
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    if job.get("status") != "ready":
        return {"ok": False, "error": f"job is {job.get('status')}, not ready to merge"}
    job["merge_requested"] = True
    _write(job)
    return {"ok": True, "id": jid}


def request_discard(jid: str) -> dict:
    """Flag a finished job for cleanup. The worker (Windows-side, owns git + disk) removes
    its worktree/branch — or its vault backup — then deletes the json+log. The dashboard
    only sets the flag, never touches git itself (the WSL↔Windows contract)."""
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    job["discard"] = True
    _write(job)
    return job


def request_vault_action(jid: str, action: str) -> dict:
    """Resolve a vault-write REVIEW (a non-git procedure that wrote status.yaml/log.md). `keep`
    finalizes the change already applied in place; `revert` restores the changed files from the
    pre-run backup. The worker (Windows-side, owns disk) does the restore; the dashboard only sets
    the flag (the WSL↔Windows contract)."""
    if action not in ("keep", "revert"):
        raise ValueError(f"vault action must be keep|revert, got {action!r}")
    job = get_job(jid)
    if not job:
        raise ValueError(f"unknown job: {jid!r}")
    job["vault_action"] = action
    _write(job)
    return job


def request_stop(jid: str) -> dict:
    """Abort a RUNNING job. Drops a `<jid>.stop` marker the worker polls mid-subprocess: it
    kills the process tree, removes the worktree/branch, and deletes the record — so all the
    abandoned work is gone. Also flags `discard` so a not-yet-running job is reclaimed too."""
    p = JOBS_DIR / f"{jid}.stop"
    p.write_text("stop", encoding="utf-8")
    job = get_job(jid)
    if job:                                   # belt + suspenders: also flag discard for the queued case
        job["discard"] = True
        _write(job)
    return {"ok": True, "id": jid}


def list_jobs() -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    jobs = []
    for f in JOBS_DIR.glob("job_*.json"):
        try:
            jobs.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return sorted(jobs, key=lambda j: j.get("created", ""), reverse=True)


def get_job(jid: str) -> dict | None:
    f = _job_path(jid)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def active() -> list[dict]:
    """Jobs in any non-terminal state (these light their project's satellite)."""
    return [j for j in list_jobs() if j.get("status") in ACTIVE_STATES]
