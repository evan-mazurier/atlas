/* Atlas v2 shell: router, poller, api helpers, areas loader, shortcuts, attention bar
   (docs/v2/contracts.md §8.1). A screen module default-exports {id, title, key, mount(root, ctx), unmount()}. */
import * as kit from "./kit/charts.js";
import * as ui from "./kit/ui.js";

const ROUTES = [
  { id: "plan", title: "Plan", key: "p", load: () => import("./screens/plan.js") },
  { id: "team", title: "Team", key: "t", load: () => import("./screens/team.js") },
  { id: "ledger", title: "Ledger", key: "l", load: () => import("./screens/ledger.js") },
];
const DEFAULT_ROUTE = "plan";

/* ---------- api ---------- */
async function request(path, opts) {
  const r = await fetch(path, Object.assign({ cache: "no-store" }, opts));
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok || !data || data.ok === false) {
    const err = new Error((data && data.error) || ("HTTP " + r.status));
    err.status = r.status;
    err.data = data;
    throw err;
  }
  return data;
}
export const api = {
  get: (path) => request(path),
  post: (path, body) => request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) }),
};

/* ---------- areas ---------- */
let AREAS = [];
async function loadAreas() {
  try {
    const d = await api.get("/api/v2/areas");
    AREAS.length = 0;                      // in place: screens hold ctx.areas
    AREAS.push(...(d.areas || []));
    AREAS.forEach((a) => document.documentElement.style.setProperty("--a-" + a.id, a.color));
  } catch (e) { /* retried on the attention poll while empty */ }
}
function areaColor(id) { return id && AREAS.some((a) => a.id === id) ? "var(--a-" + id + ")" : "var(--ctx-2)"; }
function areaName(id) { const a = AREAS.find((x) => x.id === id); return a ? a.name : "Unassigned"; }

/* ---------- screens ---------- */
const root = document.getElementById("screen");
let current = null;     // {route, mod, cancels: []}
let navToken = 0;

function makeCtx(cancels) {
  return {
    api, areas: AREAS, areaColor, areaName, kit, ui,
    query: new URLSearchParams(location.search),
    /** Poll: runs fn now and every `ms` until the screen unmounts; a run never overlaps the previous one. */
    every(ms, fn) {
      let busy = false, dead = false;
      const run = async () => {
        if (busy || dead) return;
        busy = true;
        try { await fn(); } catch (e) { /* the screen shows its own error state */ } finally { busy = false; }
      };
      run();
      const id = setInterval(run, ms);
      const cancel = () => { dead = true; clearInterval(id); };
      cancels.push(cancel);
      return cancel;
    },
    /** Run a repaint and keep the scroll position of the screen and of its marked inner scrollers. */
    keepScroll(fn) {
      const saved = [[root, root.scrollTop, root.scrollLeft]];
      root.querySelectorAll("[data-keep-scroll]").forEach((el) => saved.push([el, el.scrollTop, el.scrollLeft]));
      fn();
      saved.forEach(([el, top, left]) => {
        if (!el.isConnected) return;
        el.scrollTop = top;
        el.scrollLeft = left;
      });
    },
  };
}

function routeFromHash() {
  const m = /^#\/([a-z]+)/.exec(location.hash);
  const r = m && ROUTES.find((x) => x.id === m[1]);
  return r || ROUTES.find((x) => x.id === DEFAULT_ROUTE);
}

function paintNav(active) {
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.id === active.id));
}

async function show() {
  const route = routeFromHash();
  const token = ++navToken;
  if (current) {
    current.cancels.forEach((c) => c());
    try { current.mod.unmount(); } catch (e) { /* a screen must not block navigation */ }
    current = null;
  }
  root.textContent = "";
  root.scrollTop = 0;
  paintNav(route);
  document.title = route.title + " | Atlas";
  let mod;
  try {
    mod = (await route.load()).default;
  } catch (e) {
    if (token !== navToken) return;
    const d = document.createElement("div");
    d.className = "err-line";
    d.textContent = "The " + route.title + " screen could not be loaded (" + (e && e.message || e) + ").";
    root.appendChild(d);
    return;
  }
  if (token !== navToken) return;
  const cancels = [];
  current = { route, mod, cancels };
  mod.mount(root, makeCtx(cancels));
  root.scrollTop = 0;                    // a screen always starts at the top
}

function trackBarHeight() {
  const bar = document.getElementById("abar");
  const set = () => document.documentElement.style.setProperty("--abar-h", bar.offsetHeight + "px");
  set();
  if (window.ResizeObserver) new ResizeObserver(set).observe(bar);
}

function buildNav() {
  const nav = document.getElementById("nav");
  ROUTES.forEach((r) => {
    const a = document.createElement("a");
    a.href = "#/" + r.id;
    a.dataset.id = r.id;
    const t = document.createElement("span");
    t.textContent = r.title;
    const k = document.createElement("kbd");
    k.textContent = r.key;
    a.append(t, k);
    nav.appendChild(a);
  });
}

function typing(el) {
  return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
}
document.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey || ev.metaKey || ev.altKey || typing(ev.target)) return;
  const r = ROUTES.find((x) => x.key === ev.key.toLowerCase());
  if (r) { ev.preventDefault(); location.hash = "#/" + r.id; }
});
window.addEventListener("hashchange", show);

/* ---------- attention bar ---------- */
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function paintDate() {
  const d = new Date();
  document.getElementById("ab-date").textContent = DAYS[d.getDay()] + " " + d.getDate() + " " + MON[d.getMonth()];
}

function paintAttention(a) {
  document.getElementById("ab-err").hidden = true;
  document.getElementById("ab-team-err").hidden = !a.team_error;
  document.getElementById("ab-working-t").textContent = a.working + (a.working === 1 ? " session working" : " sessions working");
  document.getElementById("ab-waiting").hidden = !(a.waiting > 0);
  document.getElementById("ab-waiting-t").textContent = a.waiting + " waiting";
  document.getElementById("ab-review-t").textContent = a.pending_reviews + " pending review";
}

async function pollAttention() {
  if (!AREAS.length) loadAreas();
  try {
    paintAttention(await api.get("/api/v2/attention"));
  } catch (e) {
    const el = document.getElementById("ab-err");
    el.textContent = "Attention data unavailable";
    el.hidden = false;
  }
}

/* ---------- boot ---------- */
async function boot() {
  buildNav();
  trackBarHeight();
  paintDate();
  setInterval(paintDate, 60000);
  await loadAreas();
  pollAttention();
  setInterval(pollAttention, 10000);
  if (!location.hash) location.hash = "#/" + DEFAULT_ROUTE;
  show();
}
boot();
