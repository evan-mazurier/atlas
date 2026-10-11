/* Team screen (docs/v2/contracts.md §8.4): the creatures are copied verbatim (team-creatures.js);
   the text, timeline and layout around them follow design B. Data: GET /api/v2/team every 2 s,
   repainted only when the JSON changed. */
import { T, ORDER, art, creature } from "./team-creatures.js";
import { esc, tokens, fmtT } from "../kit/format.js";
import { mountRoster } from "./team-roster.js";

let state = null;

function tierName(t) { return t.charAt(0).toUpperCase() + t.slice(1); }

export default {
  id: "team",
  title: "Team",
  key: "t",
  mount(root, ctx) {
    root.textContent = "";
    const { kit } = ctx;
    const wrap = document.createElement("div");
    wrap.className = "screen-in team-in";
    wrap.innerHTML = `
      <div class="ph"><div class="eyebrow">Team</div><h1>Claude Code team</h1>
        <p class="lede">Live view of Claude Code desktop sessions and their helpers.</p></div>
      <div class="team-err" id="team-err" hidden></div>
      <div class="team-strip" id="team-strip"></div>
      <div id="team-roster"></div>
      <div class="team-lanes" id="team-lanes"></div>
      <div class="card"><div class="ch"><div class="ct"><h3>Today</h3><p>When each helper was busy</p></div><div class="lg" id="team-lg"></div></div>
        <div class="tscroll" data-keep-scroll style="margin-top:12px"><div id="team-tl"></div></div>
        <p class="note" style="margin-top:10px;margin-bottom:0">Cost is price-weighted tokens (Sonnet = 1), from the same meter as the Ledger.</p></div>`;
    root.appendChild(wrap);
    const $ = (id) => wrap.querySelector("#" + id);
    const strip = $("team-strip"), lanesEl = $("team-lanes");
    strip.innerHTML = ORDER.map((id) => `<div class="team-mem" data-id="${id}"><span class="c"></span><span class="a">${art(id, "strip-" + id)}</span><span class="n">${T[id].name}</span></div>`).join("");
    mountRoster($("team-roster"), ctx);
    kit.legend($("team-lg"), ["opus", "sonnet", "haiku"].map((m) => ({ name: tierName(m), color: "var(--m-" + m + ")" })), "sq");

    state = { lastSig: "", tlSig: "" };

    function areaChip(id) {
      if (!id) return "";
      return `<span class="team-area"><i class="sw" style="background:${ctx.areaColor(id)}"></i>${esc(ctx.areaName(id))}</span>`;
    }
    /* ── lanes: one element per session, repainted only when that session's own data changed, so the
          creatures keep animating across polls ── */
    function laneHtml(L) {
      const steps = L.steps.map((x, j) => {
        const st = x.state === "working" ? "work" : x.state;
        const prev = j ? (L.steps[j - 1].state === "working" ? "work" : L.steps[j - 1].state) : null;
        const arrow = j ? `<span class="team-ar${(prev === "done" || prev === "work") && st === "work" ? " live" : ""}"></span>` : "";
        const label = st === "done" ? "done" : st === "work" ? "working" : "next";
        return arrow + `<div class="team-st ${st}"><span class="a">${art(x.agent, L.sid + ":" + j + ":" + x.agent)}</span><span class="n">${(T[x.agent] || {}).name || esc(x.agent)}</span><span class="d">${esc(x.what)}</span><span class="s">${label}</span></div>`;
      }).join("");
      const name = L.plan_title || L.title;     /* the linked Plan item's title wins; else the session name, else the type */
      /* working only because helpers are: the conversation replied, the helpers carry on */
      const hl = L.via_helpers ? (L.live_helpers || []) : [];
      const allBg = hl.length > 0 && hl.every((h) => h.bg);
      const via = hl.length
        ? `<span class="team-via"><span class="dot" style="color:var(--ink-2)">●</span> working · ${hl.length} ${hl.length === 1 ? "helper" : "helpers"} ${allBg ? "in the background" : "running"}</span>`
          + `<span class="team-via-names">${hl.map((h) => esc(h.name + (h.label ? ": " + h.label : ""))).join(", ")}</span>` : "";
      const head = name
        ? `<span class="ty">${esc(name)}</span><span class="wk">${esc(L.type)}</span>`
        : `<span class="ty">${esc(L.type)}</span>`;
      return `<div class="team-info">${head}${areaChip(L.area)}${via}<span class="team-tag${L.ask ? " ask" : ""}">${esc(L.tag)}</span>
        <span class="team-meta">since <b>${esc(L.since)}</b><br>so far <b>${tokens(L.cost)}</b> tok</span></div><div class="team-flow">${steps}</div>`;
    }
    function idleHtml(L) {
      const wait = L.waiting ? `<span class="team-wait"><span class="dot" style="color:var(--warn)">●</span> waiting</span>` : "";
      const name = L.plan_title || L.title;
      return (name
        ? `<b>${esc(name)}</b><span>${esc(L.type)}</span>`
        : `<b>${esc(L.type)}</b>`) + areaChip(L.area) + wait + `<span>idle since ${esc(L.idle_since)}</span>`;
    }
    function emptyHtml() {
      return `<span class="a">${creature("chief", "empty")}</span><span class="t">No Claude Code session working right now</span><span class="s">This view fills in as soon as a Claude Code desktop session starts a task.</span>`;
    }
    function paintLanes(sessions) {
      const want = [];
      if (!sessions.some((s) => s.status === "working")) want.push({ key: "~empty", cls: "team-empty", html: emptyHtml(), sig: "empty" });
      sessions.forEach((s) => {
        const working = s.status === "working";
        want.push({ key: s.sid, cls: working ? "team-lane hot" : "team-idle", html: working ? laneHtml(s) : idleHtml(s),
          sig: JSON.stringify([s.status, s.waiting, s.area, s.title, s.plan_title, s.type, s.tag, s.project, s.since, s.idle_since, s.cost, s.steps, s.via_helpers, s.live_helpers]) });
      });
      const have = new Map();
      Array.from(lanesEl.children).forEach((c) => have.set(c.dataset.key, c));
      const keep = new Set(want.map((w) => w.key));
      have.forEach((el, k) => { if (!keep.has(k)) el.remove(); });
      want.forEach((w, i) => {
        let el = have.get(w.key);
        if (!el) { el = document.createElement("div"); el.dataset.key = w.key; }
        if (el.dataset.sig !== w.sig) { el.className = w.cls; el.innerHTML = w.html; el.dataset.sig = w.sig; }
        if (lanesEl.children[i] !== el) lanesEl.insertBefore(el, lanesEl.children[i] || null);
      });
    }
    function paintTimeline(tl) {
      const sig = JSON.stringify(tl);
      if (sig === state.tlSig) return;
      state.tlSig = sig;
      const host = $("team-tl");
      host.textContent = "";
      if (!tl.rows.length) {
        const e = document.createElement("div");
        e.className = "muted";
        e.textContent = "No Claude Code activity recorded today.";
        host.appendChild(e);
        return;
      }
      kit.timelineLanes(host, {
        variant: "rows", start_h: tl.start, end_h: tl.end,
        lanes: tl.rows.map((r) => ({
          label: r.type && r.label !== r.type ? r.label + " · " + r.type : r.label, right: tokens(r.cost),
          blocks: r.segs.filter((g) => g[2] !== "idle").map((g) => ({
            start: fmtT(g[0]), end: fmtT(Math.max(g[1], g[0] + 0.02)), color: "var(--m-" + g[2] + ")",
            title: tierName(g[2]), sub: tierName(g[2]),
          })),
        })),
      });
    }
    function paint(d) {
      $("team-err").hidden = true;
      strip.querySelectorAll(".team-mem").forEach((m) => {
        const n = (d.counts || {})[m.dataset.id] || 0;
        m.classList.toggle("busy", n > 0);
        m.querySelector(".c").textContent = "×" + n;
      });
      ctx.keepScroll(() => {
        paintLanes(d.sessions || []);
        paintTimeline(d.timeline || { start: 8, end: 20, rows: [] });
      });
    }
    paintLanes([]);
    ctx.every(2000, async () => {
      try {
        const d = await ctx.api.get("/api/v2/team");
        if (!state) return;
        const sig = JSON.stringify(d);
        if (sig !== state.lastSig) { state.lastSig = sig; paint(d); }
      } catch (e) {
        const el = $("team-err");
        el.textContent = "The team log could not be read (" + (e && e.message || e) + "). Showing the last view; retrying.";
        el.hidden = false;
      }
    });
  },
  unmount() { state = null; },
};
