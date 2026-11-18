"""The Brief's "To act on" gatherer (design notes, not in this copy section 5a, contracts.md 5.3a).

One function per source, each taking explicit inputs so tests never touch live files:
  inbox(db_path, now, auth_expired=None) -> lines     finance(db_path, today) -> lines
  team(tasks, sessions, now, plan_titles=None) -> lines (a pure join over injected records)

Read-only, local only: both SQLite databases are opened `mode=ro` (never through the finance or inbox
`db.connect`, which create tables or migrate), nothing is fetched (no Gmail, bank or price call), and no
line carries a sender, subject, snippet or amount: counts, categories, dates and the operator's own task
titles only. A kind that fails drops its own line; a source is "unavailable" only when its database cannot
be opened (or, for team, its inputs cannot be read). A line is
{source, kind, text, count, date, priority, open}.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
import time
from pathlib import Path

COCKPIT = Path(__file__).resolve().parents[1]
FINANCE_DIR = COCKPIT / "finance"
DAY_S = 86400.0
ACT_DAYS = 30                 # inbox pulse: Act mail from the last 30 days
DATED_DAYS = 7
WAITING_HIGH_HOURS = 24
WAITING_MAX_DAYS = 4        # The operator, 2026-11-16: older than this since the last activity = orphaned, not listed
MAX_TITLES = 3
MAX_CATEGORIES = 3

SOURCES = ("inbox", "finance", "team")
UNAVAILABLE_TEXT = {"inbox": "Inbox unavailable", "finance": "Finance unavailable", "team": "Team unavailable"}
KIND_ORDER = ("act_mails", "act_dated", "auth_expired", "to_classify", "waiting_sessions", "unavailable")
PRIVATE_KIND_RANK = 50        # kinds of a private source sort after the public ones of its own source, in line order


class SourceUnavailable(Exception):
    """The source cannot be read at all (database cannot be opened, inputs unreadable)."""


# ── helpers ────────────────────────────────────────────────────────────────────────────────────
def line(source: str, kind: str, text: str, count=None, date=None, priority: str = "normal") -> dict:
    return {"source": source, "kind": kind, "text": text, "count": count, "date": date,
            "priority": priority, "open": source}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _days_word(n: int) -> str:
    return "today" if n == 0 else ("in 1 day" if n == 1 else f"in {n} days") if n > 0 else \
        ("1 day overdue" if n == -1 else f"{-n} days overdue")


def _more(items: list[str], cap: int) -> str:
    return ", ".join(items[:cap]) + (f" and {len(items) - cap} more" if len(items) > cap else "")


def open_ro(path) -> sqlite3.Connection:
    """Open an existing SQLite file read-only (never creates a file, a table or a migration)."""
    p = Path(path)
    if not p.is_file():
        raise SourceUnavailable(f"no database file: {p.name}")
    try:
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise SourceUnavailable(str(exc)) from None
    return conn


def _guarded(out: list, fn, *args) -> None:
    """Run one kind; a failure drops its line only."""
    try:
        res = fn(*args)
    except Exception:  # noqa: BLE001 - one broken kind never takes the others down
        return
    if res:
        out.extend(res if isinstance(res, list) else [res])


# ── inbox ──────────────────────────────────────────────────────────────────────────────────────
def _iso_day(v):
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _inbox_act(conn, now: float) -> list[dict]:
    rows = conn.execute(
        "SELECT category, date_ts, deadline FROM messages WHERE action='act' AND in_inbox=1 AND is_from_me=0 "
        "AND date_ts >= ?", (now - ACT_DAYS * DAY_S,)).fetchall()
    if not rows:
        return []
    out = []
    counts: dict[str, int] = {}
    for r in rows:
        if r["category"]:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
    cats = [c for c, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    oldest = max(0, int((now - min(r["date_ts"] for r in rows)) // DAY_S))
    bits = []
    if cats:
        bits.append("categories: " + _more(cats, MAX_CATEGORIES))
    bits.append("oldest " + ("1 day" if oldest == 1 else f"{oldest} days"))
    out.append(line("inbox", "act_mails", f"{_plural(len(rows), 'mail')} to act on ({'; '.join(bits)})", count=len(rows)))
    today = dt.datetime.fromtimestamp(now).date()
    last = today + dt.timedelta(days=DATED_DAYS)
    dated = [d for d in (_iso_day(r["deadline"]) for r in rows) if d is not None and today <= d <= last]
    days, n_dated = sorted(set(dated)), len(dated)
    if n_dated:
        out.append(line("inbox", "act_dated",
                        f"{n_dated} with a date in the next {DATED_DAYS} days: " + ", ".join(d.isoformat() for d in days[:3]),
                        count=n_dated, date=days[0].isoformat()))
    return out


def _inbox_classify(conn) -> dict | None:
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE category IS NULL AND in_inbox=1 AND is_from_me=0").fetchone()[0]
    return line("inbox", "to_classify", f"{n} to classify", count=n) if n > 0 else None


def _inbox_auth(conn, auth_expired) -> dict | None:
    if auth_expired is None:     # persisted by the inbox sync (settings.auth_expired = '1' | '0')
        row = conn.execute("SELECT value FROM settings WHERE key='auth_expired'").fetchone()
        auth_expired = bool(row and row["value"] == "1")
    return line("inbox", "auth_expired", "Gmail connection expired: reconnect", priority="high") if auth_expired else None


def inbox(db_path, now: float | None = None, auth_expired: bool | None = None) -> list[dict]:
    """Inbox lines from the last background sync. `now` is epoch seconds. `auth_expired` None reads the
    persisted setting; True/False overrides it. Raises SourceUnavailable when the database cannot be opened."""
    now = time.time() if now is None else now
    conn = open_ro(db_path)
    try:
        out: list[dict] = []
        _guarded(out, _inbox_act, conn, now)
        _guarded(out, _inbox_auth, conn, auth_expired)
        _guarded(out, _inbox_classify, conn)
        return out
    finally:
        conn.close()


# ── team ───────────────────────────────────────────────────────────────────────────────────────
def _aware(v) -> dt.datetime | None:
    if isinstance(v, dt.datetime):
        d = v
    else:
        try:
            d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return d if d.tzinfo else d.astimezone()


def _clean(title) -> str:
    t = " ".join(str(title or "").replace(" — ", ", ").replace("—", ", ").split())
    return (t[:57].rstrip() + "...") if len(t) > 60 else t


def team(tasks: list[dict], sessions: list[dict], now: dt.datetime, plan_titles: dict | None = None) -> list[dict]:
    """Sessions waiting on the operator (decision "A", 2026-11-16): a session has stopped and its task is
    still open, until the task is finished (`: team-done`, status no longer open) or the session
    is archived (live state `done`). `tasks`: ledger task records (derive.py: id, sid, status, started,
    span_s, title, session_title, plan_ref, auto). `sessions`: the live team snapshot's sessions (sid, status,
    done, title). A session counts as working, and is excluded, when the live snapshot says so; a session
    missing from the snapshot (hidden after hours idle) has stopped. Automatic sessions are not waiting on
    anyone. A task whose last activity (started + span_s) is more than WAITING_MAX_DAYS (4) days old is
    treated as orphaned and not listed. Priority high when any waited more than 24 h."""
    now = _aware(now) or dt.datetime.now().astimezone()
    live = {s.get("sid"): s for s in sessions or [] if isinstance(s, dict)}
    titles = plan_titles or {}
    rows: dict[str, tuple] = {}
    for t in tasks or []:
        if not isinstance(t, dict) or t.get("status") != "open" or not t.get("sid") or t.get("auto"):
            continue
        s = live.get(t["sid"])
        if s and (s.get("status") == "working" or s.get("done")):
            continue
        started = _aware(t.get("started"))
        try:
            last = started + dt.timedelta(seconds=float(t.get("span_s") or 0)) if started else None
        except (TypeError, ValueError):
            last = started
        title = _clean(titles.get(t.get("plan_ref")) or t.get("title") or (s or {}).get("title")
                       or t.get("session_title")) or "Untitled session"
        if last is not None and now - last > dt.timedelta(days=WAITING_MAX_DAYS):
            continue
        key = last or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        if t["sid"] not in rows or key > rows[t["sid"]][0]:
            rows[t["sid"]] = (key, title, last)
    if not rows:
        return []
    ordered = sorted(rows.values(), key=lambda r: r[0])                 # waiting the longest first
    high = any(r[2] is not None and now - r[2] > dt.timedelta(hours=WAITING_HIGH_HOURS) for r in ordered)
    n = len(ordered)
    text = f"{_plural(n, 'session')} waiting: " + _more([r[1] for r in ordered], MAX_TITLES)
    return [line("team", "waiting_sessions", text, count=n, priority="high" if high else "normal")]


def _plan_titles(sessions_or_tasks: list[dict]) -> dict:
    refs = {r.get("plan_ref") for r in sessions_or_tasks if r.get("plan_ref")}
    if not refs:
        return {}
    try:
        from . import refs as plan_refs
        return {k: v.get("title") for k, v in plan_refs.resolve_many(refs).items()}
    except Exception:  # noqa: BLE001
        return {}


def _ledger_tasks() -> list[dict]:
    """Derived ledger task records (cached by the ledger query layer; read-only)."""
    from ledger2 import query  # noqa: PLC0415 - cockpit/ is on sys.path in the server
    return query.all_tasks()


def _live_sessions(now: dt.datetime) -> list[dict]:
    """The live team state as teamlive.snapshot() builds it, without the transcript reads it does for
    cost and titles (the join needs only status and done)."""
    import teamlive  # noqa: PLC0415
    naive = now.astimezone().replace(tzinfo=None, microsecond=0)
    events = teamlive.load_events(teamlive.team_dir(), naive)
    exclude = teamlive.atlas_job_sids() | teamlive.atlas_known_sids()
    state = teamlive.build_state(events, naive, exclude, None, None, teamlive.helper_transcript_mtime)
    if not state.get("ok"):
        raise SourceUnavailable("team log unreadable")
    return state.get("sessions") or []


def gather_team(now: dt.datetime | None = None) -> list[dict]:
    """team() over the live ledger and team state. Raises SourceUnavailable when either cannot be read."""
    now = now or dt.datetime.now().astimezone()
    try:
        tasks = _ledger_tasks()
        sessions = _live_sessions(now)
    except SourceUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SourceUnavailable(type(exc).__name__) from None
    open_tasks = [t for t in tasks if isinstance(t, dict) and t.get("status") == "open"]
    return team(tasks, sessions, now, _plan_titles(open_tasks))


# ── paths and the whole list ───────────────────────────────────────────────────────────────────
def finance_db_path() -> Path:
    """ATLAS_FINANCE_DB, else cockpit/finance/finance.db (where finance db.DEFAULT_DB_PATH points)."""
    env = (os.environ.get("ATLAS_FINANCE_DB") or "").strip()
    return Path(env) if env else FINANCE_DIR / "finance.db"


def inbox_db_path() -> Path:
    """ATLAS_INBOX_DB, else <inbox home>/inbox.db as inbox/paths.py resolves it (ATLAS_INBOX_HOME aware)."""
    env = (os.environ.get("ATLAS_INBOX_DB") or "").strip()
    if env:
        return Path(env)
    from inbox import paths  # noqa: PLC0415
    return paths.db_path()


def _finance_module():
    """The private finance gatherer; ImportError when the module is absent (public build)."""
    import importlib  # noqa: PLC0415
    return importlib.import_module(f"{__package__}.brief_act_finance")


def _finance_lines(db_path, today) -> list[dict]:
    return _finance_module().finance(db_path, today)


def _source_present(src: str) -> bool:
    """The optional private panels (inbox, finance) are absent from the public build: that source is
    omitted silently (no line, no act_status entry)."""
    if src == "inbox":
        return (COCKPIT / "inbox").is_dir()
    if src == "finance":
        if not FINANCE_DIR.is_dir():
            return False
        try:
            _finance_module()
        except ModuleNotFoundError as exc:
            if exc.name == f"{__package__}.brief_act_finance":
                return False                     # the private module is absent (public build)
            return True                          # a broken private import shows as "Finance unavailable"
        except ImportError:
            return True
        return True
    return True


def gather(inbox_db=None, finance_db=None, today: dt.date | None = None, now: dt.datetime | None = None,
           auth_expired: bool | None = None, team_fn=None) -> tuple[list[dict], dict]:
    """(act lines, act_status). Each source is read separately; one that cannot be read gives a single
    "<Source> unavailable" line and status "unavailable". Order: high priority first, then the kind order
    of design notes, not in this copy. `team_fn(now)` is the injectable team source (default: the live ledger and team state)."""
    now = now or dt.datetime.now().astimezone()
    today = today or now.date()
    calls = {
        "inbox": lambda: inbox(inbox_db if inbox_db is not None else inbox_db_path(), now.timestamp(), auth_expired),
        "finance": lambda: _finance_lines(finance_db if finance_db is not None else finance_db_path(), today),
        "team": lambda: (team_fn or gather_team)(now),
    }
    lines: list[dict] = []
    status: dict[str, str] = {}
    for src in SOURCES:
        if not _source_present(src):
            continue
        try:
            lines.extend(calls[src]())
            status[src] = "ok"
        except Exception:  # noqa: BLE001 - one source never breaks the Brief
            status[src] = "unavailable"
            lines.append(line(src, "unavailable", UNAVAILABLE_TEXT[src]))
    lines.sort(key=lambda l: (l["priority"] != "high", SOURCES.index(l["source"]),
                              KIND_ORDER.index(l["kind"]) if l["kind"] in KIND_ORDER else PRIVATE_KIND_RANK))
    return lines, status
