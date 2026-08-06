"""
Atlas — Mission Control dashboard.

Reads each project's status.yaml sidecar (paths in ../registry.yaml) and renders
a single overview. Read-only: Atlas never writes to a project. The work plane
(Claude Code) owns the status files; Atlas only displays them.

Run:
    streamlit run dashboard/app.py
Works from Windows Python or WSL (Windows D:\\ paths are auto-mapped to /mnt/d).
"""
from __future__ import annotations

import os
import re
import datetime as dt
from pathlib import Path

import yaml
import streamlit as st

import assistant   # local: Ollama client (control-plane only)
import dispatcher  # local: job queue (enqueue -> Windows worker runs claude)
import usage       # local: token usage from Claude Code session logs

HERE = Path(__file__).resolve().parent
REGISTRY = HERE.parent / "registry.yaml"
TODAY = dt.date.today()


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def normalize_path(p: str) -> str:
    """Make a Windows D:\\path usable from WSL (/mnt/d/path) and vice-versa."""
    if not p:
        return p
    if os.name != "nt":
        m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
        if m:
            drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
            return f"/mnt/{drive}/{rest}"
    return p


def load_registry() -> list[dict]:
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    return data.get("projects", [])


def load_status(entry: dict) -> dict:
    path = Path(normalize_path(entry.get("status_file", "")))
    try:
        s = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        s["_ok"] = True
    except Exception as exc:  # missing / malformed sidecar -> surfaced as a red card
        s = {"_ok": False, "_error": str(exc), "project": entry.get("id", "?")}
    s["_id"] = entry.get("id", s.get("project", "?"))
    s["_parent"] = entry.get("parent")     # C-3: umbrella grouping (e.g. "orchard"); None for top-level
    if entry.get("label"):                 # registry display-label override (sidecar project untouched)
        s["project"] = entry["label"]
    return s


def days_since(d) -> int | None:
    if not d:
        return None
    try:
        if isinstance(d, dt.datetime):
            d = d.date()
        elif isinstance(d, str):
            d = dt.date.fromisoformat(d.strip())
        return (TODAY - d).days
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Atlas — Mission Control", page_icon="◈", layout="wide")

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

:root{
  --bg:#08070a; --bg2:#0e0c11; --card:#121016; --card2:#16131b;
  --line:rgba(255,45,75,.16); --line-hot:rgba(255,45,75,.55);
  --red:#ff2b3d; --red-deep:#c1121f; --red-soft:#ff647a;
  --green:#2fe3a0; --amber:#ffb020; --txt:#f3eef2; --muted:#8b8390; --dim:#5c5563;
}
.stApp{
  background:
    radial-gradient(1100px 600px at 80% -10%, rgba(193,18,31,.18), transparent 60%),
    radial-gradient(900px 500px at -10% 110%, rgba(255,43,61,.10), transparent 55%),
    linear-gradient(180deg,var(--bg),var(--bg2));
  color:var(--txt);
}
#MainMenu, header, footer{visibility:hidden;}
.block-container{padding-top:2.2rem; padding-bottom:3rem; max-width:1500px;}

/* ===== Core "brain" header — the hub everything will connect to ===== */
.hub-wrap{display:flex; justify-content:center; margin:6px 0 30px;}
.hub{position:relative; width:460px; height:460px; max-width:92vw;}
.core{position:absolute; top:50%; left:50%; width:220px; height:220px;
  transform:translate(-50%,-50%); display:grid; place-items:center; z-index:2;}

/* concentric rotating rings (radar arcs) */
.core .ring{position:absolute; border-radius:50%; border:2px solid transparent;}
.ring1{inset:0;     border-top-color:var(--red); border-right-color:rgba(255,45,75,.35);
  box-shadow:0 0 26px rgba(255,43,61,.25); animation:spin 16s linear infinite;}
.ring2{inset:30px;  border-bottom-color:var(--red-soft); border-left-color:rgba(255,100,122,.4);
  animation:spin 11s linear infinite reverse;}
.ring3{inset:58px;  border:1px dashed rgba(255,45,75,.4); animation:spin 30s linear infinite;}
.ring4{inset:44px;  border:1px solid transparent; border-top-color:rgba(255,176,32,.5);
  animation:spin 8s linear infinite reverse;}   /* amber thread */

/* breathing core + dark inner disc behind the label */
.core .glow{position:absolute; inset:0; margin:auto; width:128px; height:128px; border-radius:50%;
  background:radial-gradient(circle, rgba(255,43,61,.55), rgba(193,18,31,.16) 55%, transparent 72%);
  filter:blur(4px); animation:breathe 3.6s ease-in-out infinite;}
.core .disc{position:absolute; inset:0; margin:auto; width:112px; height:112px; border-radius:50%;
  background:radial-gradient(circle at 50% 32%, #1c1016, #0b090d 72%);
  border:1px solid rgba(255,45,75,.3); box-shadow:inset 0 0 24px rgba(255,43,61,.35);}

/* orbiting nodes — placeholders for connected entities (projects/sessions/metrics) */
.orbit{position:absolute; border-radius:50%; animation:spin linear infinite;}
.o1{inset:4px;  animation-duration:9s;}
.o2{inset:34px; animation-duration:15s; animation-direction:reverse;}
.o3{inset:64px; animation-duration:22s;}
.node{position:absolute; top:-5px; left:50%; margin-left:-5px; width:10px; height:10px;
  border-radius:50%; background:var(--red); box-shadow:0 0 12px var(--red);}
.node.green{background:var(--green); box-shadow:0 0 12px var(--green);}
.node.amber{background:var(--amber); box-shadow:0 0 12px var(--amber);}

.core .label{position:relative; z-index:3; text-align:center;}
.label .t{font-family:'Orbitron'; font-weight:900; font-size:1.75rem; letter-spacing:.18em;
  background:linear-gradient(90deg,#fff,var(--red-soft) 60%,var(--red));
  -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;
  text-shadow:0 0 26px rgba(255,43,61,.5);}
.label .s{font-family:'Rajdhani'; font-size:.6rem; letter-spacing:.34em; color:var(--muted);
  text-transform:uppercase; margin-top:3px;}

@keyframes spin{to{transform:rotate(360deg);}}
@keyframes breathe{0%,100%{transform:scale(.9); opacity:.7;}50%{transform:scale(1.08); opacity:1;}}

/* satellites = one per project, orbiting the core; click → open its panel */
.spoke{position:absolute; top:50%; left:50%; height:1px; width:150px; z-index:1;
  transform-origin:left center;
  background:linear-gradient(90deg, rgba(255,45,75,.45), rgba(255,45,75,0));}
.sat{position:absolute; top:50%; left:50%; width:0; height:0; z-index:4; text-decoration:none;
  transform:rotate(var(--ang)) translate(var(--r)) rotate(calc(-1 * var(--ang)));}
.sat .dot{position:absolute; left:-10px; top:-10px; width:20px; height:20px; border-radius:50%;
  border:2px solid rgba(8,7,10,.85); transition:.18s; animation:satpulse 2.8s ease-in-out infinite;}
.sat .lbl{position:absolute; left:50%; top:15px; transform:translateX(-50%); white-space:nowrap;
  font-family:'Rajdhani'; font-weight:600; font-size:.84rem; letter-spacing:.03em;
  color:var(--muted); transition:.18s;}
.sat:hover .dot{transform:scale(1.28);}
.sat:hover .lbl{color:var(--txt);}
.sat.on .dot{transform:scale(1.32); box-shadow:0 0 0 4px rgba(255,45,75,.18), 0 0 18px rgba(255,45,75,.6);}
.sat.on .lbl{color:#fff;}
@keyframes satpulse{0%,100%{opacity:.78;}50%{opacity:1;}}

.backlink{display:inline-block; margin:0 0 14px; font-family:'JetBrains Mono'; font-size:.78rem;
  letter-spacing:.06em; color:var(--red-soft); text-decoration:none; border:1px solid var(--line);
  border-radius:8px; padding:6px 12px; background:var(--card);}
.backlink:hover{border-color:var(--line-hot); color:#fff;}
.grid.focus{grid-template-columns:1fr; max-width:660px; margin:0 auto;}

/* Summary chips */
.sumbar{display:flex; gap:14px; margin-bottom:26px; flex-wrap:wrap;}
.sumchip{font-family:'Rajdhani'; font-weight:600; letter-spacing:.06em;
  border:1px solid var(--line); border-radius:10px; padding:8px 16px; background:var(--card);
  display:flex; align-items:center; gap:9px; font-size:.95rem;}
.sumchip b{font-family:'Orbitron'; font-size:1.05rem;}

/* Grid */
.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:22px;}

/* C-3: Orchard umbrella — a bordered section grouping its sub-project cards */
.grp{border:1px solid var(--line-hot); border-radius:18px; padding:16px 18px 20px; margin-bottom:26px;
  background:linear-gradient(180deg, rgba(193,18,31,.06), transparent 62%);}
.grp-h{display:flex; align-items:center; gap:12px; margin-bottom:16px;}
.grp-name{font-family:'Orbitron'; font-weight:700; font-size:1.35rem; letter-spacing:.06em;}
.grp-kind{font-family:'JetBrains Mono'; font-size:.66rem; color:var(--dim);
  text-transform:uppercase; letter-spacing:.12em;}
.grp-blk{font-family:'JetBrains Mono'; font-size:.72rem; color:#ffd2d8; margin-left:auto;}
.grp-grid{grid-template-columns:repeat(auto-fill,minmax(380px,1fr));}

/* Card */
.card{position:relative; background:linear-gradient(180deg,var(--card),var(--card2));
  border:1px solid var(--line); border-radius:16px; padding:20px 22px 18px;
  overflow:hidden; transition:.18s; box-shadow:0 14px 40px rgba(0,0,0,.45);}
.card:hover{border-color:var(--line-hot); transform:translateY(-3px);
  box-shadow:0 18px 52px rgba(193,18,31,.28);}
.card::before{content:''; position:absolute; top:0; left:0; right:0; height:3px;
  background:linear-gradient(90deg,var(--red),var(--red-deep),transparent);}

.c-top{display:flex; align-items:center; justify-content:space-between; gap:10px;}
.c-name{font-family:'Orbitron'; font-weight:700; font-size:1.18rem; letter-spacing:.04em;}
.c-kind{font-family:'JetBrains Mono'; font-size:.68rem; color:var(--dim);
  text-transform:uppercase; letter-spacing:.12em; margin-top:3px;}
.dot{width:11px; height:11px; border-radius:50%; display:inline-block;}
.dot-green{background:var(--green); box-shadow:0 0 12px var(--green); animation:pulse 2.4s infinite;}
.dot-amber{background:var(--amber); box-shadow:0 0 12px var(--amber); animation:pulse 2.4s infinite;}
.dot-red{background:var(--red); box-shadow:0 0 14px var(--red); animation:pulse 1.4s infinite;}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.35;}}

.c-phase{font-family:'Rajdhani'; font-weight:600; font-size:1.02rem; color:var(--red-soft);
  margin:14px 0 2px;}
.c-status{font-family:'JetBrains Mono'; font-size:.72rem; color:var(--muted);
  text-transform:uppercase; letter-spacing:.1em;}

.c-label{font-family:'JetBrains Mono'; font-size:.64rem; letter-spacing:.16em;
  text-transform:uppercase; color:var(--dim); margin:16px 0 5px;}
.c-next{font-family:'Rajdhani'; font-size:1.0rem; font-weight:500; line-height:1.4; color:var(--txt);
  border-left:2px solid var(--red); padding-left:11px;}

.chips{display:flex; flex-wrap:wrap; gap:7px; margin-top:7px;}
.chip{font-family:'JetBrains Mono'; font-size:.74rem; padding:4px 9px; border-radius:7px;
  background:rgba(255,45,75,.07); border:1px solid var(--line); color:var(--txt);}
.chip span{color:var(--muted);}

.tracks{display:flex; flex-direction:column; gap:5px; margin-top:6px;}
.track{font-family:'Rajdhani'; font-size:.92rem; color:var(--txt);}
.track b{color:var(--red-soft); font-weight:600;}
.track i{color:var(--muted); font-style:normal;}

.blk{font-family:'Rajdhani'; font-size:.9rem; color:#ffd2d8; line-height:1.45;
  padding-left:14px; position:relative; margin-top:4px;}
.blk::before{content:'\\25B8'; position:absolute; left:0; color:var(--red);}

.c-foot{display:flex; justify-content:space-between; align-items:center;
  margin-top:18px; padding-top:12px; border-top:1px solid rgba(255,255,255,.05);}
.c-sess{font-family:'Rajdhani'; font-size:.84rem; color:var(--muted); line-height:1.35;
  max-width:70%;}
.age{font-family:'JetBrains Mono'; font-size:.7rem; padding:3px 9px; border-radius:20px;
  white-space:nowrap;}
.age-fresh{color:var(--green); border:1px solid rgba(47,227,160,.3);}
.age-warn{color:var(--amber); border:1px solid rgba(255,176,32,.35);}
.age-stale{color:var(--red); border:1px solid rgba(255,43,61,.4);}
.card-err{border-color:var(--line-hot);}
.card-err .c-phase{color:var(--red);}

/* ===== talk-to-the-core chat ===== */
.ask-hint{text-align:center; font-family:'JetBrains Mono'; color:var(--dim);
  letter-spacing:.22em; font-size:.62rem; text-transform:uppercase; margin:-4px 0 8px;}
div[data-testid="stForm"]{border:none!important; padding:0!important;}
.stTextInput input{background:var(--card)!important; color:var(--txt)!important;
  border:1px solid var(--line)!important; border-radius:10px!important;
  font-family:'Rajdhani'!important; font-size:1rem!important;}
.stTextInput input:focus{border-color:var(--line-hot)!important;
  box-shadow:0 0 0 2px rgba(255,45,75,.12)!important;}
div[data-testid="stFormSubmitButton"] button{
  background:linear-gradient(180deg,var(--red),var(--red-deep))!important; color:#fff!important;
  border:none!important; border-radius:10px!important; font-family:'Rajdhani'!important;
  font-weight:600!important; letter-spacing:.06em!important;}
div[data-testid="stFormSubmitButton"] button:hover{filter:brightness(1.12)!important;}
.ans{max-width:660px; margin:10px auto 24px;
  background:linear-gradient(180deg,var(--card),var(--card2));
  border:1px solid var(--line); border-left:3px solid var(--red); border-radius:12px;
  padding:13px 18px; font-family:'Rajdhani'; font-size:1rem; line-height:1.5; color:var(--txt);}
.ans .who{font-family:'JetBrains Mono'; font-size:.6rem; letter-spacing:.16em;
  text-transform:uppercase; color:var(--red-soft); margin-bottom:6px;}
.ans.err{border-left-color:var(--amber);} .ans.err .who{color:var(--amber);}

/* ===== dispatch: busy nodes, usage strip, confirm, sessions ===== */
.sat.busy .dot{animation:satbusy 1s ease-in-out infinite;
  box-shadow:0 0 0 3px rgba(255,45,75,.25), 0 0 18px var(--red) !important;}
@keyframes satbusy{0%,100%{transform:scale(1);}50%{transform:scale(1.4);}}

.usage{display:flex; align-items:center; gap:16px; flex-wrap:wrap; max-width:760px;
  margin:2px auto 18px; padding:8px 16px; border:1px solid var(--line); border-radius:10px;
  background:var(--card); font-family:'JetBrains Mono'; font-size:.72rem; color:var(--muted);}
.usage .ul{letter-spacing:.12em; text-transform:uppercase; color:var(--dim);}
.usage .uv b{color:var(--red-soft); font-size:.95rem;}
.usage .uspark{display:flex; align-items:flex-end; gap:2px; height:24px;}
.usage .ubar{width:7px; background:linear-gradient(180deg,var(--red),var(--red-deep)); border-radius:2px;}

.confirm-h{font-family:'Orbitron'; font-size:.92rem; color:var(--red-soft); letter-spacing:.05em;
  margin:6px 0 8px; text-align:center;}
.confirm-meta{font-family:'JetBrains Mono'; font-size:.74rem; color:var(--muted); margin:6px 0 10px;}
.confirm-meta b{color:var(--txt);}

.sess-h{font-family:'Orbitron'; font-size:.95rem; color:var(--red-soft); letter-spacing:.08em;
  margin:8px 0 10px;}
.sess{display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:12px; margin-bottom:24px;}
.jcard{background:var(--card); border:1px solid var(--line); border-left:3px solid var(--red);
  border-radius:10px; padding:11px 14px; font-family:'Rajdhani';}
.jtop{display:flex; align-items:center; gap:8px; font-size:.95rem;}
.jmeta{font-family:'JetBrains Mono'; font-size:.64rem; color:var(--dim);}
.jstatus{margin-left:auto; font-family:'JetBrains Mono'; font-size:.62rem; text-transform:uppercase;
  letter-spacing:.1em; color:var(--muted);}
.jwhen{font-family:'JetBrains Mono'; font-size:.62rem; color:var(--dim);}
.jtask{font-size:.9rem; color:var(--txt); margin:6px 0; line-height:1.35;}
.jres summary{cursor:pointer; color:var(--red-soft); font-family:'JetBrains Mono'; font-size:.64rem;
  text-transform:uppercase; letter-spacing:.1em;}
.jres div{margin-top:6px; color:var(--muted); line-height:1.45; font-size:.88rem;}
.jerr{font-size:.84rem; color:#ffd2d8; margin-top:4px;}
.jq{font-size:.86rem; color:#ffe6b0; margin:4px 0 2px; line-height:1.4;
  border-left:2px solid var(--amber, #e0a23c); padding-left:8px;}
.ansq{font-size:.82rem; color:#ffe6b0; margin:10px 0 4px; line-height:1.4;}
.jfoot{font-family:'JetBrains Mono'; font-size:.62rem; color:var(--dim); margin-top:8px;}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Render helpers
# --------------------------------------------------------------------------- #
def esc(x) -> str:
    return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def age_badge(updated) -> str:
    n = days_since(updated)
    if n is None:
        return '<span class="age age-stale">no date</span>'
    cls = "age-fresh" if n <= 2 else "age-warn" if n <= 6 else "age-stale"
    label = "today" if n == 0 else "1d ago" if n == 1 else f"{n}d ago"
    return f'<span class="age {cls}">{esc(updated)} · {label}</span>'


def render_card(s: dict) -> str:
    if not s.get("_ok", False):
        return (f'<div class="card card-err"><div class="c-top"><div>'
                f'<div class="c-name">{esc(s.get("_id"))}</div>'
                f'<div class="c-kind">status file unreadable</div></div>'
                f'<span class="dot dot-red"></span></div>'
                f'<div class="c-phase">⚠ {esc(s.get("_error",""))[:120]}</div></div>')

    health = str(s.get("health", "amber")).lower()
    dot = {"green": "dot-green", "amber": "dot-amber", "red": "dot-red"}.get(health, "dot-amber")

    # metrics chips
    chips = ""
    for k, v in (s.get("metrics") or {}).items():
        chips += f'<div class="chip"><span>{esc(k)}</span> {esc(v)}</div>'
    chips_html = f'<div class="c-label">Metrics</div><div class="chips">{chips}</div>' if chips else ""

    # tracks
    tracks_html = ""
    if s.get("tracks"):
        rows = "".join(
            f'<div class="track"><b>{esc(t.get("name"))}</b> '
            f'<i>· {esc(t.get("status",""))}</i> — {esc(t.get("next",""))}</div>'
            for t in s["tracks"]
        )
        tracks_html = f'<div class="c-label">Tracks</div><div class="tracks">{rows}</div>'

    # blockers
    blk = s.get("blockers") or []
    blk_html = ""
    if blk:
        items = "".join(f'<div class="blk">{esc(b)}</div>' for b in blk)
        blk_html = f'<div class="c-label">Blockers ({len(blk)})</div>{items}'

    return (
        f'<div class="card">'
        f'<div class="c-top"><div>'
        f'<div class="c-name">{esc(s.get("project","?"))}</div>'
        f'<div class="c-kind">{esc(s.get("kind",""))}</div></div>'
        f'<span class="dot {dot}"></span></div>'
        f'<div class="c-phase">{esc(s.get("phase",""))}</div>'
        f'<div class="c-status">{esc(s.get("status",""))}</div>'
        f'<div class="c-label">Next target</div>'
        f'<div class="c-next">{esc(s.get("next_target","—"))}</div>'
        f'{tracks_html}{chips_html}{blk_html}'
        f'<div class="c-foot"><div class="c-sess">{esc(s.get("last_session",""))}</div>'
        f'{age_badge(s.get("updated"))}</div>'
        f'</div>'
    )


def render_group(name: str, children: list[dict]) -> str:
    """C-3: wrap a `parent` group (e.g. Orchard) as one bordered section containing its
    sub-project cards. Rollup health = worst child; blocker count = sum across children."""
    rank = {"green": 0, "amber": 1, "red": 2}
    healths = [str(c.get("health", "amber")).lower() for c in children if c.get("_ok")]
    worst = max(healths, key=lambda h: rank.get(h, 1)) if healths else "amber"
    dot = {"green": "dot-green", "amber": "dot-amber", "red": "dot-red"}.get(worst, "dot-amber")
    total_blk = sum(len(c.get("blockers") or []) for c in children if c.get("_ok"))
    blk = f'<span class="grp-blk">⚠ {total_blk} open blockers</span>' if total_blk else ""
    cards = "".join(render_card(c) for c in children)
    return (
        f'<div class="grp">'
        f'<div class="grp-h"><span class="dot {dot}"></span>'
        f'<span class="grp-name">{esc(name)}</span>'
        f'<span class="grp-kind">umbrella · {len(children)} projects</span>{blk}</div>'
        f'<div class="grid grp-grid">{cards}</div>'
        f'</div>'
    )


def render_job(j: dict) -> str:
    scls = {"queued": "dot-amber", "scoping": "dot-amber", "coding": "dot-amber",
            "testing": "dot-amber", "review": "dot-amber", "reworking": "dot-amber",
            "needs_input": "dot-amber", "done": "dot-green", "ready": "dot-green",
            "merged": "dot-green", "failed": "dot-red"}.get(j.get("status"), "dot-amber")
    meta = f'{esc(j.get("session_type") or "—")} · {esc(j.get("model") or "?")}/{esc(j.get("effort") or "?")}'
    when = esc((j.get("finished") or j.get("started") or j.get("created") or "")[11:16])
    if j.get("result"):
        body = (f'<details class="jres"><summary>result</summary>'
                f'<div>{esc(j["result"])}</div></details>')
    elif j.get("error"):
        body = f'<div class="jerr">{esc(j["error"])[:300]}</div>'
    else:
        body = ""
    if j.get("status") == "needs_input" and j.get("question"):
        body = (f'<div class="jq">❔ {esc(j["question"])}</div>' + body)
    elif j.get("status") == "needs_input" and j.get("contract") and not j.get("contract_confirmed"):
        body = ('<div class="jq">▣ contract proposed — review &amp; confirm below</div>' + body)
    elif j.get("status") == "ready" and j.get("branch"):
        body = (f'<div class="jq">✓ gate passed — committed on <code>{esc(j["branch"])}</code>. '
                f'Merge when your tree is clean:<br>'
                f'<code>git -C {esc(j.get("cwd",""))} merge {esc(j["branch"])}</code></div>' + body)
    if j.get("review_notes") and j.get("status") in ("ready", "needs_input"):
        body = (f'<details class="jres"><summary>review notes</summary>'
                f'<div>{esc(j["review_notes"])}</div></details>' + body)
    foot = []
    if j.get("branch"):
        foot.append("⎇ " + esc(j["branch"]))
    if j.get("review_verdict"):
        foot.append("⚖ " + esc(j["review_verdict"]))
    if j.get("commit"):
        foot.append("⬡ " + esc(j["commit"]))
    if j.get("rework_count"):
        foot.append(f'↻ {j["rework_count"]}')
    if j.get("diffstat") and j["diffstat"] != "(no file changes)":
        foot.append(esc(j["diffstat"].splitlines()[-1].strip()))
    if j.get("tokens"):
        foot.append(f'{usage.human(j["tokens"])} tok')
    if j.get("worktree_cleaned"):
        foot.append("⌫ worktree reclaimed")
    footh = " · ".join(foot)
    return (f'<div class="jcard"><div class="jtop"><span class="dot {scls}"></span>'
            f'<b>{esc(j.get("project"))}</b><span class="jmeta">{meta}</span>'
            f'<span class="jstatus">{esc(j.get("status"))}</span>'
            f'<span class="jwhen">{when}</span></div>'
            f'<div class="jtask">{esc((j.get("task") or "")[:160])}</div>{body}'
            + (f'<div class="jfoot">{footh}</div>' if footh else "") + '</div>')


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
statuses = [load_status(e) for e in load_registry()]

# Which project (if any) is focused — driven by the ?focus=<id> URL param that the
# satellites set on click. Later, the Ollama assistant can open a panel the same way
# (by setting this param), so voice/text and clicks share one routing mechanism.
focus = st.query_params.get("focus")
ids = {s.get("_id") for s in statuses}
try:
    busy_projects = {j.get("project") for j in dispatcher.active()}
except Exception:  # noqa: BLE001
    busy_projects = set()


@st.cache_data(ttl=45)
def _usage_snapshot():
    return usage.collect(days=7)


def health_color(h):
    return {"green": "#2fe3a0", "amber": "#ffb020", "red": "#ff2b3d"}.get(str(h).lower(), "#ff2b3d")

# The core: animated hub of the system, with ONE satellite per project orbiting it
# (health-colored, click → opens that project's panel). The ambient inner dots stay
# as A future home for finer-grained entities (running sessions, revenue, goals...).
n = max(len(statuses), 1)
spokes = sats = ""
for i, s in enumerate(statuses):
    ang = (360.0 / n) * i - 90            # evenly spaced, first node at the top
    hid = s.get("_id")
    col = health_color(s.get("health")) if s.get("_ok") else "#ff2b3d"
    on = " on" if focus == hid else ""
    busy = " busy" if hid in busy_projects else ""
    spokes += f'<div class="spoke" style="transform:rotate({ang}deg)"></div>'
    sats += (
        f'<a class="sat{on}{busy}" href="?focus={hid}" target="_self" style="--ang:{ang}deg;--r:150px;">'
        f'<span class="dot" style="background:{col};box-shadow:0 0 14px {col}"></span>'
        f'<span class="lbl">{esc(s.get("project", hid))}</span></a>'
    )

core_html = (
    '<div class="core">'
    '<div class="ring ring1"></div><div class="ring ring2"></div>'
    '<div class="ring ring3"></div><div class="ring ring4"></div>'
    '<div class="glow"></div><div class="disc"></div>'
    '<div class="orbit o1"><span class="node"></span></div>'
    '<div class="orbit o2"><span class="node green"></span></div>'
    '<div class="orbit o3"><span class="node amber"></span></div>'
    '<div class="label"><div class="t">ATLAS</div>'
    '<div class="s">Mission Control</div></div>'
    '</div>'
)
st.markdown(f'<div class="hub-wrap"><div class="hub">{spokes}{core_html}{sats}</div></div>',
            unsafe_allow_html=True)

# ---- talk to the core: ask the local model, grounded in live state; it can open a panel ----
st.session_state.setdefault("chat_hist", [])
st.session_state.setdefault("last_q", "")
st.session_state.setdefault("last_a", "")
st.session_state.setdefault("last_err", "")
st.session_state.setdefault("pending_dispatch", None)

st.markdown('<div class="ask-hint">— talk to the core —</div>', unsafe_allow_html=True)
_l, _mid, _r = st.columns([1, 3, 1])
with _mid:
    with st.form("ask_core", clear_on_submit=True):
        q = st.text_input("ask", label_visibility="collapsed",
                          placeholder="Ask the core…   e.g.  what's blocking Almanac?")
        sent = st.form_submit_button("Ask the core", use_container_width=True)

if sent and q.strip():
    with st.spinner("the core is thinking…"):
        res = assistant.ask(q.strip(), statuses, history=st.session_state.chat_hist)
    ans, act, err = res.get("answer") or "", res.get("action"), res.get("error")
    st.session_state.last_q, st.session_state.last_err = q.strip(), err or ""
    if act and act[0] == "focus":
        st.session_state.last_a = ans or f"Opening {act[1]}."
        st.session_state.chat_hist = (st.session_state.chat_hist + [
            {"role": "user", "content": q.strip()},
            {"role": "assistant", "content": st.session_state.last_a}])[-8:]
        st.query_params["focus"] = act[1]
        st.rerun()
    elif act and act[0] == "clear":
        st.session_state.last_a = ans or "Showing all projects."
        st.query_params.clear()
        st.rerun()
    elif act and act[0] == "dispatch":
        st.session_state.pending_dispatch = act[1]
        st.session_state.cd_brief = act[1]["task"]
        st.session_state.last_a = ans or (
            f"Proposed a session for {act[1]['project']} — review the brief and confirm below.")
        st.session_state.chat_hist = (st.session_state.chat_hist + [
            {"role": "user", "content": q.strip()},
            {"role": "assistant", "content": st.session_state.last_a}])[-8:]
    else:
        st.session_state.last_a = ans
        if ans:
            st.session_state.chat_hist = (st.session_state.chat_hist + [
                {"role": "user", "content": q.strip()},
                {"role": "assistant", "content": ans}])[-8:]

if st.session_state.last_err:
    st.markdown(f'<div class="ans err"><div class="who">Atlas · offline</div>'
                f'{esc(st.session_state.last_err)}</div>', unsafe_allow_html=True)
elif st.session_state.last_a:
    st.markdown(f'<div class="ans"><div class="who">Atlas &nbsp;·&nbsp; you asked: '
                f'{esc(st.session_state.last_q)}</div>{esc(st.session_state.last_a)}</div>',
                unsafe_allow_html=True)

# ---- manual dispatch (deterministic path — no local model needed) ----
_pids = [s.get("_id") for s in statuses if s.get("_id")]
_ldc, _cdc, _rdc = st.columns([1, 3, 1])
with _cdc:
    with st.expander("＋ New dispatch"):
        _ndp = st.selectbox("Project", _pids, key="nd_proj")
        _ndt_opts = dispatcher.session_types().get(_ndp, []) or ["(default)"]
        _ndt = st.selectbox("Session type", _ndt_opts, key="nd_type")
        _ndg = st.text_area("Goal / brief", key="nd_goal", height=90,
                            placeholder="What should the session do?")
        if st.button("Prepare dispatch →", key="nd_go", use_container_width=True) and _ndg.strip():
            # route through the same confirm card (scope toggle + model preview) as the chat path
            st.session_state.pending_dispatch = {
                "project": _ndp,
                "session_type": None if _ndt == "(default)" else _ndt,
                "task": _ndg.strip(),
            }
            st.session_state.cd_brief = _ndg.strip()
            st.rerun()

# ---- dispatch confirmation (Gate 1: confirm the brief before it runs) ----
pd = st.session_state.get("pending_dispatch")
if pd:
    _lc, _cc, _rc = st.columns([1, 3, 1])
    with _cc:
        st.markdown('<div class="confirm-h">▣ Confirm dispatch — autonomous Claude Code session</div>',
                    unsafe_allow_html=True)
        opts = dispatcher.session_types().get(pd["project"], []) or ["(default)"]
        idx = opts.index(pd["session_type"]) if pd.get("session_type") in opts else 0
        sel_type = st.selectbox(f"Session type · {pd['project']}", opts, index=idx, key="cd_type")
        st.text_area("Goal / brief", key="cd_brief", height=130)
        scope_first = st.checkbox(
            "Scope it first — interactive Opus session interrogates you, then proposes a "
            "contract to confirm (recommended for real implementation work)",
            value=True, key="cd_scope")
        st_arg = None if sel_type == "(default)" else sel_type
        rp = dispatcher.resolve_procedure(pd["project"], st_arg)
        st.markdown(f'<div class="confirm-meta">→ <b>{esc(pd["project"])}</b> · '
                    + ('scoping <b>opus</b>/<b>high</b> then ' if scope_first else '')
                    + f'coding model <b>{esc(rp["model"])}</b> · effort <b>{esc(rp["effort"])}</b>'
                    + (f' · {esc(rp["desc"])}' if rp.get("desc") else "") + '</div>',
                    unsafe_allow_html=True)
        _d, _x = st.columns(2)
        _label = "⟫ Begin scoping" if scope_first else "⟫ Dispatch"
        if _d.button(_label, use_container_width=True, type="primary"):
            brief = (st.session_state.get("cd_brief") or "").strip()
            if brief:
                job = dispatcher.enqueue(pd["project"], brief, st_arg, scope=scope_first)
                st.session_state.pending_dispatch = None
                st.session_state.last_a = (
                    (f"Scoping session queued for {pd['project']} (opus/high) — it will "
                     "interrogate you, then propose a contract to confirm.") if scope_first else
                    (f"Dispatched to {pd['project']} · {job['model']}/{job['effort']}. "
                     "Job queued — the worker will run it."))
                st.rerun()
        if _x.button("Cancel", use_container_width=True):
            st.session_state.pending_dispatch = None
            st.rerun()

# ---- usage meter (token consumption from local Claude Code logs) ----
_u = _usage_snapshot()
if _u.get("ok"):
    _mx = max((d["total"] for d in _u["series"]), default=1) or 1
    _spark = "".join(
        f'<span class="ubar" style="height:{4 + int(20 * d["total"] / _mx)}px" '
        f'title="{d["day"]}: {usage.human(d["total"])}"></span>' for d in _u["series"])
    _models = "   ".join(f'{esc(m)} {usage.human(n)}' for m, n in _u["top_models"])
    st.markdown(
        '<div class="usage"><span class="ul">USAGE · Claude Code (local · tokens)</span>'
        f'<span class="uv">today <b>{usage.human(_u["today_total"])}</b></span>'
        f'<span class="uv">7d <b>{usage.human(_u["week_total"])}</b></span>'
        f'<span class="uspark">{_spark}</span><span class="um">{_models}</span></div>',
        unsafe_allow_html=True)

# summary bar
ok = [s for s in statuses if s.get("_ok")]
def count(h): return sum(1 for s in ok if str(s.get("health", "")).lower() == h)
total_blk = sum(len(s.get("blockers") or []) for s in ok)
st.markdown(
    f'<div class="sumbar">'
    f'<div class="sumchip"><b>{len(statuses)}</b> projects</div>'
    f'<div class="sumchip"><span class="dot dot-green"></span><b>{count("green")}</b> on track</div>'
    f'<div class="sumchip"><span class="dot dot-amber"></span><b>{count("amber")}</b> watch</div>'
    f'<div class="sumchip"><span class="dot dot-red"></span><b>{count("red")}</b> blocked</div>'
    f'<div class="sumchip">⚠ <b>{total_blk}</b> open blockers</div>'
    f'</div>',
    unsafe_allow_html=True,
)

# ---- sessions panel: dispatched Claude Code jobs ----
_jobs = dispatcher.list_jobs()[:8]
if _jobs:
    st.markdown('<div class="sess-h">◇ Sessions</div>', unsafe_allow_html=True)
    st.markdown('<div class="sess">' + "".join(render_job(j) for j in _jobs) + '</div>',
                unsafe_allow_html=True)
    # Two kinds of parked (needs_input) session:
    #  (a) a proposed CONTRACT awaiting the operator's confirm (gate #1) → editable confirm card;
    #  (b) an in-flight question (escalation / scoping interrogation) → answer box, which
    #      re-queues the job so the worker resumes it via `claude --resume`.
    for j in (x for x in _jobs if x.get("status") == "needs_input"):
        if j.get("contract") and not j.get("contract_confirmed"):
            c = j["contract"]
            with st.form(key=f"ct_{j['id']}", clear_on_submit=False):
                st.markdown(f'<div class="ansq">▣ <b>{esc(j.get("project"))}</b> — proposed '
                            'contract. Review, edit if needed, and confirm to dispatch coding.</div>',
                            unsafe_allow_html=True)
                cb = st.text_area("Brief", value=c.get("brief", ""),
                                  key=f"ctb_{j['id']}", height=140)
                cc = st.text_area("Acceptance criteria (one per line)",
                                  value="\n".join(c.get("acceptance_criteria") or []),
                                  key=f"ctc_{j['id']}", height=90)
                ct = st.text_input("Test command (hard gate — blank = review-only)",
                                   value=c.get("test_cmd") or (j.get("test_cmd") or ""),
                                   key=f"ctt_{j['id']}")
                if st.form_submit_button("✓ Confirm contract → dispatch coding", type="primary"):
                    contract = {
                        "brief": cb.strip(),
                        "acceptance_criteria": [ln.strip() for ln in cc.splitlines() if ln.strip()],
                        "test_cmd": ct.strip() or None,
                    }
                    if contract["brief"]:
                        dispatcher.confirm_contract(j["id"], contract)
                        st.session_state.last_a = (f"Contract confirmed for {j['project']} — "
                                                   "coding session queued.")
                        st.rerun()
        else:
            with st.form(key=f"ans_{j['id']}", clear_on_submit=True):
                st.markdown(f'<div class="ansq">❔ <b>{esc(j.get("project"))}</b> asks: '
                            f'{esc(j.get("question") or "")}</div>', unsafe_allow_html=True)
                ans = st.text_area("Your answer", key=f"ansbox_{j['id']}",
                                   label_visibility="collapsed", height=80,
                                   placeholder="Answer the session's question — it resumes with this.")
                if st.form_submit_button("⟫ Send & resume", type="primary") and ans.strip():
                    dispatcher.answer_job(j["id"], ans.strip())
                    st.session_state.last_a = f"Answered {j['project']} — resuming the session."
                    st.rerun()
    # A gated 'ready' job is committed on atlas/<id> awaiting your merge (gate #2). Once you've
    # merged it yourself, mark it merged (record-keeping only — Atlas never merges a live branch).
    for j in (x for x in _jobs if x.get("status") == "ready"):
        if st.button(f"✓ mark merged · {esc(j.get('project'))} {j['id'][-4:]}",
                     key=f"merged_{j['id']}", help="You merged atlas/<id> manually → flip to merged"):
            dispatcher.mark_merged(j["id"])
            st.session_state.last_a = f"Marked {j['project']} session merged."
            st.rerun()
    # Discard a finished session → flags it; the worker cleans the worktree + deletes the record.
    _fin = [x for x in _jobs if x.get("status") in ("done", "failed", "merged", "ready")
            and not x.get("discard")]
    if _fin:
        _dcols = st.columns(4)
        for _i, _j in enumerate(_fin):
            if _dcols[_i % 4].button(f"✕ {esc(_j.get('project'))} {_j['id'][-4:]}",
                                     key=f"disc_{_j['id']}", help="Discard: clean worktree + remove record"):
                dispatcher.request_discard(_j["id"])
                st.session_state.last_a = (f"Discarding {_j['project']} session — the worker will "
                                           "clean its worktree and remove the record.")
                st.rerun()

if focus and focus in ids:
    # a satellite was clicked → show just that project's panel, full width
    fs = next(s for s in statuses if s.get("_id") == focus)
    st.markdown('<a class="backlink" href="?" target="_self">‹ All projects</a>',
                unsafe_allow_html=True)
    st.markdown(f'<div class="grid focus">{render_card(fs)}</div>', unsafe_allow_html=True)
else:
    # C-3: fold parent:orchard children under one "Orchard" umbrella section; other projects
    # stay top-level peers below it. (Focus view above is unchanged — a child focuses as itself.)
    children = [s for s in statuses if s.get("_parent") == "orchard"]
    others = [s for s in statuses if s.get("_parent") != "orchard"]
    if children:
        st.markdown(render_group("Orchard", children), unsafe_allow_html=True)
    if others:
        cards = "".join(render_card(s) for s in others)
        st.markdown(f'<div class="grid">{cards}</div>', unsafe_allow_html=True)

st.markdown(
    f'<div style="margin-top:34px;font-family:JetBrains Mono;font-size:.7rem;'
    f'color:#5c5563;letter-spacing:.1em;">ATLAS v0 · read-only · '
    f'{len(statuses)} sidecars · {TODAY.isoformat()}</div>',
    unsafe_allow_html=True,
)
