/* Team screen (docs/v2/contracts.md §8.4): the creatures are copied verbatim (team-creatures.js);
   the text, timeline and layout around them follow design B. Each session's helpers are drawn as a chart of runs
   (team-graph.js, grouped by task, no arrows), changed on 2026-11-16 at the operator's request. Data: GET /api/v2/team every 2 s,
   repainted only when the JSON changed. */
import { T, ORDER, art, creature } from "./team-creatures.js";
import { graphHtml, viewOf, wheelView, panView, keyView, boundsOf, repaintPlot, updateNow, setHot, openMap, LEFT_W, PLOT_PAD } from "./team-graph.js";
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
    kit.legend($("team-lg"), ["opus", "sonnet", "haiku", "fable"].map((m) => ({ name: tierName(m), color: "var(--m-" + m + ")" })), "sq");

    state = { lastSig: "", tlSig: "", open: new Set(), sessions: [], nowH: null, folds: new Map(), views: new Map(), plotW: 520 };
    /* the chart's options for one session: plot width (label placement), the operator's fold toggles (per sid and task)
       and the zoom/pan window set by hand (per sid, kept in memory across polls; absent = the default window, which follows now) */
    const chartOpts = (L) => ({ plotW: state.plotW, folds: state.folds.get(L.sid), view: state.views.get(L.sid) });

    function areaChip(id) {
      if (!id) return "";
      return `<span class="team-area"><i class="sw" style="background:${ctx.areaColor(id)}"></i>${esc(ctx.areaName(id))}</span>`;
    }
    /* ── lanes: one element per session, repainted only when that session's own data changed, so the
          creatures keep animating across polls ── */
    function laneHtml(L) {
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
        <span class="team-meta">since <b>${esc(L.since)}</b> · so far <b>${tokens(L.cost)}</b> tok</span></div>${graphHtml(L, state.nowH, chartOpts(L))}`;
    }
    function idleHtml(L) {
      const wait = L.waiting ? `<span class="team-wait"><span class="dot" style="color:var(--warn)">●</span> waiting</span>`
        : L.done ? `<span class="team-done">done</span>` : "";
      const name = L.plan_title || L.title;
      const open = state.open.has(L.sid);
      const toggle = (L.helpers || []).length
        ? `<button type="button" class="tg-toggle" data-sid="${esc(L.sid)}" aria-expanded="${open}"><span class="chev" aria-hidden="true"></span>${open ? "Hide" : "Show how it ran"}</button>` : "";
      return (name
        ? `<b>${esc(name)}</b><span>${esc(L.type)}</span>`
        : `<b>${esc(L.type)}</b>`) + areaChip(L.area) + wait + `<span>idle since ${esc(L.idle_since)}</span>` + toggle
        + (open ? `<div class="tg-idle-chart">${graphHtml(L, state.nowH, chartOpts(L))}</div>` : "");
    }
    function emptyHtml() {
      return `<span class="a">${creature("chief", "empty")}</span><span class="t">No Claude Code session working right now</span><span class="s">This view fills in as soon as a Claude Code desktop session starts a task.</span>`;
    }
    /* the repaint signature of a lane: its own data, fold state and plot width, and the default window (5-minute steps) while
       the view is not set by hand; a manual zoom/pan is painted in place and never changes the signature */
    function sigOf(s) {
      const working = s.status === "working", has = (s.helpers || []).length > 0;
      return JSON.stringify([s.status, s.waiting, s.done, s.area, s.title, s.plan_title, s.type, s.tag, s.project, s.since, s.idle_since, s.cost, s.via_helpers, s.live_helpers,
        s.helpers, s.helpers_total, s.tasks, has ? (state.views.has(s.sid) ? "manual" : viewOf(s, state.nowH, null)) : null, !working && state.open.has(s.sid),
        has ? [...openMap(s, state.folds.get(s.sid))] : null, state.plotW]);
    }
    function paintLanes(sessions) {
      state.sessions = sessions;
      const lw = lanesEl.clientWidth;
      state.plotW = lw ? Math.max(300, Math.round((lw - 34 - LEFT_W - PLOT_PAD) / 20) * 20) : 520;
      const want = [];
      if (!sessions.some((s) => s.status === "working")) want.push({ key: "~empty", cls: "team-empty", html: emptyHtml(), sig: "empty" });
      sessions.forEach((s) => {
        const working = s.status === "working";
        want.push({ key: s.sid, cls: working ? "team-lane hot" : "team-idle", html: working ? laneHtml(s) : idleHtml(s), sig: sigOf(s) });
      });
      const have = new Map();
      Array.from(lanesEl.children).forEach((c) => have.set(c.dataset.key, c));
      const keep = new Set(want.map((w) => w.key));
      have.forEach((el, k) => { if (!keep.has(k)) el.remove(); });
      want.forEach((w, i) => {
        let el = have.get(w.key);
        if (!el) { el = document.createElement("div"); el.dataset.key = w.key; }
        if (el.dataset.sig !== w.sig) {
          const hadFocus = !!(document.activeElement && document.activeElement.classList && document.activeElement.classList.contains("tg-zthumb") && el.contains(document.activeElement));
          el.className = w.cls; el.innerHTML = w.html; el.dataset.sig = w.sig;
          if (hadFocus) {                    /* the thumb was replaced: keep keyboard focus on the new one (and the bar visible) */
            const th = el.querySelector(".tg-zthumb");
            if (th) { const bar = th.closest(".tg-zbar"); if (bar) bar.hidden = false; th.focus(); }
          }
        }
        if (lanesEl.children[i] !== el) lanesEl.insertBefore(el, lanesEl.children[i] || null);
      });
      /* running bars grow with the clock: moved in place, so the creatures are not repainted */
      sessions.forEach((s) => {
        const el = have.get(s.sid) || Array.from(lanesEl.children).find((c) => c.dataset.key === s.sid);
        if (el && (s.status === "working" || state.open.has(s.sid))) updateNow(el, s, state.nowH);
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
      const m = /T(\d\d):(\d\d):?(\d\d)?/.exec(d.now || "");
      state.nowH = m ? Number(m[1]) + Number(m[2]) / 60 + Number(m[3] || 0) / 3600 : null;
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
    /* ── zoom and pan: only the SVG of the lane is redrawn (requestAnimationFrame), so the creatures keep running ── */
    const pending = new Set();
    let raf = 0;
    const laneEl = (sid) => Array.from(lanesEl.children).find((c) => c.dataset.key === sid);
    const sessOf = (sid) => state.sessions.find((x) => x.sid === sid);
    const flush = () => {
      raf = 0;
      if (!state) return;
      pending.forEach((sid) => {
        const el = laneEl(sid), s = sessOf(sid);
        if (!el || !s) return;
        repaintPlot(el, s, state.nowH, chartOpts(s));
        el.dataset.sig = sigOf(s);
      });
      pending.clear();
    };
    const queue = (sid) => { pending.add(sid); if (!raf) raf = requestAnimationFrame(flush); };
    const setView = (sid, v) => { if (v) state.views.set(sid, v); else state.views.delete(sid); queue(sid); };
    const curView = (s) => viewOf(s, state.nowH, state.views.get(s.sid));
    const sidOf = (t) => { const l = t.closest("[data-key]"); return l ? l.dataset.key : null; };
    const drag = (ev, onMove) => {       /* window-level listeners: the SVG is replaced while the pointer moves */
      const x0 = ev.clientX;
      const move = (e) => onMove(e.clientX - x0);
      const up = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); window.removeEventListener("pointercancel", up); onMove(null); };
      window.addEventListener("pointermove", move); window.addEventListener("pointerup", up); window.addEventListener("pointercancel", up);
      ev.preventDefault();
    };
    /* the listeners sit on the lanes container and find the plot through .tg-svgbox, which a redraw never replaces
       (the <svg> inside it is replaced on every zoom/pan step) */
    lanesEl.addEventListener("wheel", (ev) => {
      const box = ev.target.closest && ev.target.closest(".tg-svgbox");
      const sid = box && sidOf(box), s = sid && sessOf(sid);
      if (!s) return;                              /* anywhere else the page scrolls as usual */
      ev.preventDefault();
      const r = box.getBoundingClientRect();
      setView(sid, wheelView(s, state.nowH, curView(s), ev, r.left, r.width));
    }, { passive: false });
    lanesEl.addEventListener("pointerdown", (ev) => {
      if (ev.button != null && ev.button !== 0) return;
      const t = ev.target;
      const svg = t.closest && t.closest(".tg-svgbox"), thumb = t.closest && t.closest(".tg-zthumb"), track = t.closest && t.closest(".tg-ztrack");
      const sid = (svg || track) && sidOf(svg || track), s = sid && sessOf(sid);
      if (!s) return;
      const start = curView(s), w = start.t1 - start.t0;
      if (svg) {                                   /* drag the plot: the time under the pointer follows it */
        const pw = svg.getBoundingClientRect().width || 1, tg = svg.closest(".tg");
        if (tg) tg.classList.add("panning");
        drag(ev, (dx) => { if (dx == null) { if (tg) tg.classList.remove("panning"); return; } setView(sid, panView(s, state.nowH, start, -dx / pw * w)); });
      } else if (thumb) {
        const tw = track.getBoundingClientRect().width || 1, { lo, hi } = boundsOf(s, state.nowH);
        thumb.classList.add("drag");
        drag(ev, (dx) => { if (dx == null) { thumb.classList.remove("drag"); return; } setView(sid, panView(s, state.nowH, start, dx / tw * (hi - lo))); });
      } else {                                     /* click on the track: the window centres on that time */
        const r = track.getBoundingClientRect(), { lo, hi } = boundsOf(s, state.nowH);
        const c = lo + Math.max(0, Math.min(1, (ev.clientX - r.left) / (r.width || 1))) * (hi - lo);
        setView(sid, panView(s, state.nowH, start, c - (start.t0 + w / 2)));
      }
    });
    lanesEl.addEventListener("keydown", (ev) => {     /* the focused time bar thumb; any other key is left alone */
      const thumb = ev.target.closest && ev.target.closest(".tg-zthumb");
      if (!thumb || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      const sid = sidOf(thumb), s = sid && sessOf(sid);
      if (!s) return;
      const r = keyView(s, state.nowH, curView(s), ev.key);
      if (!r) return;
      ev.preventDefault();
      setView(sid, r.view);
    });
    lanesEl.addEventListener("dblclick", (ev) => {
      const box = ev.target.closest && ev.target.closest(".tg-svgbox");
      const sid = box && sidOf(box);
      if (sid) setView(sid, null);
    });
    lanesEl.addEventListener("click", (ev) => {
      const rb = ev.target.closest && ev.target.closest(".tg-reset");
      if (rb && lanesEl.contains(rb)) { const sid = sidOf(rb); if (sid) setView(sid, null); return; }
      const gt = ev.target.closest && ev.target.closest(".tg-gt");
      if (gt && lanesEl.contains(gt)) {
        const sess = state.sessions.find((x) => x.sid === gt.dataset.sid);
        if (!sess) return;
        const cur = openMap(sess, state.folds.get(sess.sid)).get(gt.dataset.gk);
        if (!state.folds.has(sess.sid)) state.folds.set(sess.sid, new Map());
        state.folds.get(sess.sid).set(gt.dataset.gk, !cur);
        ctx.keepScroll(() => paintLanes(state.sessions));
        return;
      }
      const b = ev.target.closest && ev.target.closest(".tg-toggle");
      if (!b || !lanesEl.contains(b)) return;
      const sid = b.dataset.sid;
      if (state.open.has(sid)) state.open.delete(sid); else state.open.add(sid);
      ctx.keepScroll(() => paintLanes(state.sessions));
    });
    lanesEl.addEventListener("mouseover", (ev) => {
      const t = ev.target.closest && ev.target.closest(".tg-row[data-r], .tg-bar[data-r]");
      const lane = t && t.closest("[data-key]");
      if (lane) setHot(lane, t.dataset.r);
    });
    lanesEl.addEventListener("mouseout", (ev) => {
      const t = ev.target.closest && ev.target.closest(".tg-row[data-r], .tg-bar[data-r]");
      const lane = t && t.closest("[data-key]");
      if (lane && !(ev.relatedTarget && t.contains(ev.relatedTarget))) setHot(lane, null);
    });
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
