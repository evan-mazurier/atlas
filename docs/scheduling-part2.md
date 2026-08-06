# Atlas — Scheduling Layer, Part 2 (Atlas everywhere, beyond the work plane)

> The locked design of record for **Part 2** of the time layer: Atlas reachable and controllable
> from anywhere, and the operator's **non-work events** as first-class typed items — *replacing Google
> Calendar entirely*. Scoped with the operator **2026-07-19**. Companion to `scheduling.md` (Part 1, the
> native scheduling core) and `planner.md` (milestones/tasks/the planner agent). **Read `scheduling.md`
> first** — this doc extends its `schedule.yaml` model and assumes its day-view/session machinery.
>
> **Status (2026-07-19): Tier A shipped; phone slice next; two pieces deferred until on site.**
> **Tier A (the life-events data model + desktop capture) is BUILT + MERGED** (§12.A). With the phone in
> hand, the **phone slice** (Tailscale desktop↔phone + the Android PWA: read, capture, push reminders) is
> the **next build and is fully testable anywhere** (Tailscale is location-independent — §12.B). Only the
> **WoL relay** (needs the destination LAN) and the **laptop remote-desktop** (needs a laptop not yet
> owned) are **deferred until on site** (§12.C). One decision to settle first: the phone reminder
> mechanism (§9/§13 — Web Push, not local).

---

## 1. The gap this closes — and the reframe

Part 1 gave Atlas a native, sovereign model of **time** (availability + intra-day placement of work),
but it is **desktop-bound and work-only**: it runs on one machine, and it models project work, not the
rest of the operator's life. Two things are still missing:

1. **Reach.** Atlas can only be seen and driven at the desk. The operator wants to *work* on Atlas from a
   laptop anywhere (borrowing the desktop's compute), and *glance at / capture into* the plan from his
   phone.
2. **Everything outside the work plane.** The operator still keeps a **separate calendar** for anything
   that is not project work (appointments, errands, protected free time). Part 1's availability is a bare
   type-less denylist, so it cannot replace that calendar — it can only describe blocked hours.

### 1a. The reframe — NOT "run Atlas on a VPS" (the key scoping decision, recorded)

Part 1's §12 said "run Atlas on a **VPS**." **Scoping revealed that phrase is wrong for what the operator
actually needed.** The goal is not to *move* Atlas to the cloud — it is for the **desktop to stay the
sovereign compute core** (GPUs, local agents, models, the model CLI, repos, the source of truth)
and to **reach that one instance remotely**. The laptop *borrows the desktop's power*; it does not run
its own Atlas. So P2 is **remote access to a single always-on-capable desktop instance**, not a
migration.

This collapses the design and makes it *maximally* north-star (`CLAUDE.md` — locally sovereign):
**no third party ever runs the workload.** The only cloud-shaped thing is an encrypted tunnel to
The operator's own machine. Two facts make it clean:
- **Atlas is already a set of web servers** (control center `server.py` :8765, Streamlit dashboard :8501) —
  a remote client is already the natural shape; nothing is re-architected to be "networked."
- **Reachability is solved by a mesh VPN, not a host** (§3) — no port-forwarding, no public exposure,
  works behind any consumer router (including carrier-supplied ones).

---

## 2. The three-tier topology (locked)

| Tier | Role | What it runs | How it connects |
| --- | --- | --- | --- |
| **Desktop** | The sovereign **core** — compute + source of truth | All of Atlas: worker, agents, models (Ollama/llama.cpp), `claude` CLI, repos, `schedule.yaml`/`plan.yaml`, the control center + dashboard web servers | **Asleep by default** (§4); woken on demand |
| **Laptop** | Full **remote workstation** | Nothing but a client — **borrows the desktop's compute** | **Remote-desktop** into the desktop (§6) |
| **Phone** | Light **planning window** | An installable **PWA** — read + capture life-events (§7) | Browser/PWA to the desktop over the mesh (§3) |

The desktop is the single instance; laptop and phone are **remote clients of it**, not second copies.
There is **no multi-instance sync of Atlas itself** — the only thing that syncs is phone-captured
life-events flushing into the desktop's `schedule.yaml` (§7).

---

## 3. The fabric — a WireGuard mesh (Tailscale) (locked)

The secure connective tissue across desktop + laptop + phone is a **WireGuard mesh via Tailscale**:
- **Free** for personal use; **works behind any router** (fibre included) via NAT traversal — no
  port-forwarding, nothing exposed to the public internet.
- Each device gets a **stable private address**; the laptop/phone reach the desktop's existing web
  servers directly (`http://<desktop-tailnet-ip>:8765`, etc.).
- **North-star fit:** an overlay that only *connects the operator's own devices* — no third party hosts or
  sees the workload. (Longer-term, raw WireGuard is the sovereign fallback; Tailscale is the friendly
  wrapper, same as Ollama-over-llama.cpp.)

> **Not the same as NordVPN.** The NordLynx/NordVPN already installed is a commercial *exit* VPN (it
> masks outbound traffic); it does **not** let the operator's devices reach each other. Tailscale is the right,
> different tool, and the two coexist.

**Security posture:** the tailnet is the only ingress; Tailscale ACLs restrict which device can reach
which service (phone → read/capture API only; laptop → full). Nothing is published to the open
internet. WoL (§4) is triggerable only from within the tailnet/LAN.

---

## 4. The desktop core — asleep by default, woken on demand (locked)

Leaving a GPU desktop powered 24/7 is the real cost the operator wants to avoid (electricity). So the desktop
**sleeps (S3) or powers off by default** and is **woken remotely via Wake-on-LAN (WoL)** when the operator sits
down to work — locally or from the laptop.

**Verified already (properties of the machine, so they travel with it):**
- Desktop is on **wired Ethernet** (Intel I219-V) — the reliable case for WoL (far better than WiFi).
- That NIC is **already armed to wake** the machine (`powercfg /devicequery wake_armed`).
- **S3 sleep** is available (the WoL-friendly state).

**One gotcha to handle at build:** Windows **Fast Startup** is on, which can block WoL from a *full
shutdown*. Waking from **S3 sleep** works; if wake-from-full-shutdown is wanted, disable Fast Startup.

**The catch WoL always has:** a magic packet is normally LAN-local, so waking the desktop *from the
internet* needs **something always-on inside the home** to emit it — see §5.

---

## 5. The always-on relay — the ONE open decision (deferred until on site) 🔶

To wake the desktop from afar (and to anchor the tailnet / optionally buffer phone captures), one small
element must stay awake on the home LAN. **The concrete choice depends on the router at the destination and
must be tested on-site — it cannot be decided now** (the router in front of the machine today tells you nothing about the one it will sit behind).

Preference order (cheapest/most-sovereign first):
1. **Router-native WoL** — if the router can send a WoL magic packet from the internet (or run a
   tailnet node), **$0, no new hardware.** *(Consumer routers are highly variable on this and mostly cannot run Tailscale
   themselves — hence the on-site test.)* **← try the $0 path first.**
2. **Tiny always-on device** (Raspberry Pi, ~$50 once, ~3W ≈ pennies/month) on the home LAN — plays
   **three roles at once**: WoL sender + tailnet anchor + (optional) buffer/mirror of the schedule for
   the phone when the desktop is off. The reliable fallback; fully at-home = north-star.
3. **Small cloud VPS** — always-on wake trigger + phone mirror regardless of home hardware, but a
   recurring cost and a third party in the loop (**least** north-star). Last resort.

**Design impact is contained:** the rest of P2 treats the relay as an abstraction — *"an always-on LAN
element that (a) wakes the desktop and (b) anchors the tailnet."* Whichever option wins, nothing else
in this doc changes. Note that if phone-capture uses the **phone-local-queue** model (§7, recommended),
the relay does **not** need to buffer captures — the phone does — which keeps option 1 viable.

---

## 6. The laptop surface — remote-desktop (locked; tool recommended)

The laptop is a **remote-desktop** into the woken desktop. Chosen over a browser-over-tailnet client
because the operator wants "as simple as possible" + "use the desktop's power": remote-desktop makes
**everything work with zero adaptation** — the native control center, dispatch, **and voice** (mic/audio ride
the session) — because you are literally driving the desktop's screen, on its GPU.

- **Recommended stack: Sunshine (host on the desktop) + Moonlight (client on the laptop).** Free,
  open-source, **self-hosted over the tailnet**, uses the desktop's GPU encoder — **maximally
  sovereign** (no third-party relay). Gaming-grade low latency, fine for real work.
- **Turnkey fallback: Parsec.** Equally free for personal use and near-zero-setup, but routes through
  Parsec's servers/relay (a third party in the path) — use only if Sunshine setup proves fiddly.

No Atlas code changes are required for the laptop tier — it is pure infrastructure.

---

## 7. The phone surface — an installable PWA: read + capture (locked)

The phone is a **Progressive Web App** served from the desktop's control center over the tailnet, installable
to the home screen. Scope (chosen 2026-07-19): **read + capture life-events** — a genuine, if light,
Google-Calendar replacement, not a read-only mirror.

- **Read.** Shows today/week: the placed work-sessions, availability, life-events, at-risk, next
  targets — the planning glance. Consistent with the control center's visual language (§11 of `scheduling.md`).
- **Local cache (service worker).** The PWA caches the schedule locally, so it **works while the desktop
  is asleep** and **fires reminders locally** (§9) — **no push-notification server, no cost.**
- **Capture.** Quick-add availability + life-events ("busy 3–5pm Thu", "dentist Tue 10am"). Captures go
  into a **local queue on the phone** and **sync into the desktop's `schedule.yaml`** the next time the
  desktop is reachable (woken, or the operator is home). The phone is **authoritative for its own un-synced
  captures**; the desktop merges them on reconnect.
- **Why phone-local-queue (recommended):** it needs **no always-on server** for capture — the phone
  holds pending writes itself — keeping the whole design device-local and sovereign, and keeping relay
  **option 1** (§5) viable. *(Alternative if ever wanted: the always-on relay buffers captures; more
  moving parts, only worth it if multi-device capture consistency matters. Flagged, not chosen.)*

`schedule.yaml` on the desktop remains the **single source of truth**; the phone queue is a staging
buffer that always reconciles into it.

---

## 8. The life-events model — `schedule.yaml` gains typed events (locked shape)

Part 1's availability was a **type-less denylist** (`blocks` + `full_days`). Part 2 adds **first-class
typed life-events**, and unifies them with availability: a *busy* event automatically carves
availability, so the scheduler (§7 of `scheduling.md`) needs no new logic — it still just avoids
unavailable time.

```yaml
# schedule.yaml — Part 2 adds a first-class `events:` section (personal / life). The desktop owns and
# writes it (phone captures reconcile in). Availability is now fed by BOTH manual blocks and busy events.

events:
  - id: evt_dentist
    title: "Dentist"
    day: 2026-08-04
    start: "15:00"
    end: "15:45"            # omit start+end for an all-day event
    type: appointment        # appointment | errand | personal  (all occupy your time = block work)
    remind: 30m              # OPTIONAL reminder lead-time; fires locally on the phone (§9). omit = none
    location: ""             # OPTIONAL
    recur:                   # OPTIONAL simple recurrence (RRULE-lite) — see §8a
      freq: weekly           # daily | weekly | monthly
      interval: 1            # every N periods
      until: 2026-12-31      # or `count: N`; omit both = indefinite
    source: phone            # provenance: phone | desktop (for reconcile/debug)
    notes: ""
```

Rules of the model (locked):
- **Every life-event carves availability.** An event is treated by the scheduler exactly like an
  availability `block` — **no task is ever placed on it** (`scheduling.md` §3/§7 hard constraint,
  unchanged). Part 1's manual `blocks`/`full_days` **stay** for pure "unavailable, no event meaning"
  (you're away); life-events add "I have a thing here." Both feed **one** derived work-denylist.
- **Types are semantic/display only** (appointment/errand/personal) — they label the event for the operator;
  they do **not** change scheduler behavior (all block work equally). Keep the set small; more can be
  added later.
- **Events reference nothing** — unlike placements (which reference project tasks), life-events are
  self-contained. They live only in `schedule.yaml`.

> **Decision 2026-07-19 — dropped the `free`/`busy` distinction.** An earlier draft had a `type: free`
> ("protected time" the scheduler must not fill) and a `busy` flag. The operator cut it: to the scheduler
> "protected free time" is **indistinguishable from unavailable** (both just block work), and it was
> visually redundant with a `personal` event (which already occupies time and shows the same way). So
> there is no `free` type and no `busy` flag — **all life-events block work.** *(The genuinely distinct
> standard-calendar concept — a **transparent** event that shows on the calendar but does* not *block
> work, e.g. a birthday/FYI — is the opposite of the old `free` and is parked (§14), to add only if a
> real need appears.)*

### 8a. Recurrence (RRULE-lite) — recommended in v1 🔶

A calendar you *replace* must handle "every Tuesday standup," so v1 includes **simple recurrence**
(`freq` daily/weekly/monthly + `interval` + `until`/`count`). Deliberately a small subset of iCal
RRULE — not arbitrary rules. Recurring events **expand deterministically** in the loader (like
placements) into concrete day instances for the day-view, availability denylist, and reminders.
*(Recommendation — if the operator prefers, v1 ships one-off events only and recurrence is a fast follow.)*

---

## 9. Notifications — reminders must fire on the phone (mechanism NEEDS a scoping pass 🔶)

Replacing Google Calendar means **event reminders must fire on the phone**, including when Atlas isn't
open.

> **Correction 2026-07-19 — the original "phone-local, no server push" plan does NOT hold.** A
> closed/backgrounded PWA **cannot reliably schedule a future local notification** — the web platform has
> no durable "fire this at 3pm" API (the experimental Notification Triggers API never shipped; iOS
> forbids it outright; Android throttles background timers). So a reminder that fires **when Atlas isn't
> open must be delivered by a PUSH** to the phone at reminder time — *not* purely local.
>
> **the operator's phone is a Samsung (Android)** — good: installed Android PWAs support **Web Push** via the
> browser's push service, so this is feasible. The **push sender must be awake at reminder time**: the
> **desktop while it's on** (works today, wherever it is), and the **relay when the desktop is asleep**
> (ties back to §5, the destination). The desktop reminder built in Tier A (a browser `Notification` while the
> control center tab is open) is the desktop half; the phone push is the always-on half.
>
> **A short scoping pass settles the exact mechanism before the phone build (§13):** who runs the push
> sender, the push-subscription flow on the installed PWA, and the desktop-on vs desktop-asleep senders.
> The GOAL is unchanged (reminders fire on the phone, sovereign); only the *how* moves from "local" to
> "self-hosted push."

---

## 10. Google Calendar replacement — clean cutover (locked)

Chosen 2026-07-19: **clean cutover, no import.** Atlas becomes the **only** calendar. The operator **re-enters
his (few) recurring life-events fresh** into the new `events:` model. **Atlas never touches the Google
API** — no adapter, no import, no OAuth, ever. This is the most sovereign path and finally makes Atlas
the single place both the work and the life live.

*(The §1a/§13 read-only Google import from Part 1 is now moot and dropped — clean cutover supersedes
it.)*

---

## 11. Consistency mandate

Same as `scheduling.md` §11 / `planner.md` §8, verbatim in spirit. Every new surface — the phone PWA's
day/week view, the life-event chips, the capture form — MUST reuse the existing control center visual language
(CSS variables, Orbitron/Rajdhani/JetBrains Mono, chip/card idioms, rounding, glow, scroll/click-to-open
UX). The phone view is a **responsive variant** of the existing day-view, not a new visual language.
Life-events get an on-theme treatment distinct from both the at-risk **red** and the blocked-time device
(`scheduling.md` §9 color constraint holds).

---

## 12. Build order — RE-SEQUENCED 2026-07-19

**What changed:** Tier A (the life-events data model / desktop capture — old step 3) **shipped**, and
The operator **has the phone in hand**. Since Tailscale is location-independent, the **phone slice is now the
next build and is fully testable anywhere** (zero rework later). Only the two pieces that genuinely
need the destination network / hardware — the **WoL relay** and the **laptop remote-desktop** — defer to
the destination. New order:

- **A. Life-events data model + desktop capture (§8) — ✅ DONE, shipped + merged 2026-07-19** (Tier A):
  typed `events:` (appointment/errand/personal — `free`/`busy` dropped) + RRULE-lite recurrence + the
  day-view (timed chips, all-day = full column) + the themed capture form (calendar picker) + a
  **desktop** reminder (browser `Notification` while the control center is open). Assistant can't clobber events.

- **B. THE PHONE SLICE — ▶ NEXT, buildable + testable anywhere (has the phone).** Do a short **reminder
  scoping pass first (§9/§13)**, then build:
  - **B1. Fabric.** Tailscale on **desktop + phone** (laptop later); ACLs (phone = read/capture). Verify:
    the phone reaches the control center over the tailnet, nothing exposed publicly. *(Works on the the current location
    network — Tailscale is an overlay, not site-dependent.)*
  - **B2. Phone PWA — read.** Responsive day/week view served over the tailnet, installable, service-worker
    cache. Verify on the real phone: glance works; renders from cache when the desktop is unreachable.
  - **B3. Phone PWA — capture + sync.** Quick-add life-events/availability → phone-local queue → reconcile
    into the desktop `schedule.yaml` on reconnect. Verify: capture, then confirm it lands on the desktop.
  - **B4. Always-on phone reminders (§9).** Web Push to the installed Android PWA at reminder time, sent by
    the awake desktop (desktop-on case). Verify a reminder fires on the phone with the control center
    closed. *(The desktop-asleep sender is the relay → completed on site with C1.)*

- **C. SITE-GATED (deferred — needs the destination LAN / the not-yet-owned laptop):**
  - **C1. WoL relay + desktop-asleep (§5).** Confirm S3-wake on the real destination LAN; test router
    WoL-over-internet; else add the tiny Pi. Then wire the asleep-desktop push sender (§9) through it.
  - **C2. Laptop remote-desktop.** Sunshine (desktop) + Moonlight (laptop, **not yet owned**) over the
    tailnet. Verify full native control center + voice from the laptop, desktop woken via C1.

---

## 13. Open decisions (flagged — resolve before/at build)

- 🔶 **Phone reminder mechanism (§9) — resolve in a short scoping pass BEFORE the phone build (B4).** A
  closed PWA can't fire scheduled local notifications, so always-on reminders need **Web Push** (feasible
  on the operator's Android). Decide: the push-subscription flow on the installed PWA, who runs the push sender
  (desktop-on now; relay when asleep, C1), and keeping it self-hosted/sovereign. Goal unchanged;
  only the "how" moves from local to push.
- 🔶 **The always-on relay (§5, C1)** — router-native WoL vs tiny Pi vs VPS. **Deferred to on-site
  testing at the destination.** Try the $0 router path first.
- ✅ **Recurrence depth (§8a)** — RRULE-lite shipped in Tier A. *(Resolved.)*
- 🔶 **Capture sync model (§7)** — phone-local-queue (recommended, no always-on store) vs relay-buffered.
  Recommended path chosen unless multi-device capture consistency later demands otherwise.

## 14. Parked (recorded so nothing is lost)

- **Two-way sync with an external calendar** — obsolete under clean cutover (§10); dropped, not merely
  deferred.
- **Wake-the-desktop-for-every-capture** — rejected in favour of the phone-local queue (§7); waking a
  GPU desktop to jot "dentist 3pm" is absurd.
- **Server/cloud push notifications** — not needed; phone-local reminders (§9) cover it at $0.
- **Transparent (non-blocking) events** — a standard-calendar event that shows on the calendar but does
  **not** block work-scheduling (a birthday, an FYI, a tentative hold). This is the genuinely-distinct
  concept the dropped `free` type was *not* (see §8 decision 2026-07-19). Add only if a real need
  appears; would reintroduce a per-event "blocks work?" flag, defaulting to true.
- **Richer event fields** (attendees, invites, RSVP, attachments) — Atlas is a *personal* time model,
  not a groupware calendar; add only if a real need appears.
- **Multi-user / sharing** — out of scope; Atlas is the operator's.
- **Browser-over-tailnet laptop client** — the more "app-native" alternative to remote-desktop (§6),
  parked because it strands voice; revisit only if remote-desktop proves unsatisfactory.
