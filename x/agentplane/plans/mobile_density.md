# Mobile/compact UI density

Status: **captured, not designed.** Nothing below is a committed direction yet — this is the
operator's raw list plus the concrete current-code anchors for each item, so a later pass can pick
items up without re-deriving where they live. No implementation until this gets a real design pass.

## Problem

The app (`app/frontend/`) reads fine on desktop but wastes vertical space on mobile: a fixed
five-button top nav, a session header row that fights a 280px model `Select` for space, and
full-text status badges throughout the transcript. Inspiration named explicitly: Claude.ai's mobile
layout, which tucks the model picker and send/stop control under the composer instead of into a
persistent header row.

## Top-level nav — `app.tsx`

`AppRoutes` renders one `Group` of five always-visible buttons: Sandboxes, Actions, Connections,
MCP servers, Notifications (`app.tsx:70-98`). Proposal to evaluate: keep Sandboxes and Actions as
primary (they're the operational loop — see below on Actions specifically), move Connections, MCP
servers, and Notifications under a settings/overflow menu. That alone removes 3 of 5 buttons from
the permanent top row.

Whether "Sandboxes" stays the primary landing button at all is exactly the question in
[session-first navigation](session_first_navigation.md) — a bigger, separate redesign; don't block
this smaller nav cleanup on that decision.

## Session header row — `session.tsx:404-430`

One `Group` currently holds: back button, editable thread title, status `Badge`, harness `Badge`,
a 280px-wide model `Select`, a shutdown `ActionIcon`, and a "Raw frames" `Switch`. This is the
worst offender on narrow screens — it's already wrapping/crowding before any transcript content
renders.

- **"Raw frames" switch** → the user's "raw packets under a hamburger": this toggle switches the
  whole transcript into protocol-frame view (`timeline(state)` at `session.tsx:439-445`, backed by
  `FrameView` from `frame.tsx`). It's a debugging affordance, not a primary control — move it into
  a menu (session-level overflow, or the same settings surface as above) rather than a persistent
  header switch.
- **Model picker + stop button** → mirror Claude.ai's mobile pattern: relocate the model `Select`
  and the interrupt `ActionIcon` (`IconPlayerStop`, `session.tsx:490-498`) to sit adjacent to the
  composer (`session.tsx:477-499`) rather than in the top header, at least at narrow widths. Desktop
  can plausibly keep them in the header if there's room; mobile shouldn't have to.

  **Landed** (transcript-restyle PR): the model picker and stop control now sit in a row under the
  composer textarea on every width, not just narrow ones.

- Shutdown (`IconPower`, `aria-label="Shut down harness"`, `session.tsx:461-470`) and the harness
  badge are lower-frequency actions/status — candidates for the same overflow treatment as the
  raw-frames switch. **Not to be confused with the interrupt button below** — two separate
  "stop"-shaped controls, different scope: this one tears down the whole harness process; the one
  under the composer only stops the current turn. Its low frequency plus destructiveness makes it a
  _better_ overflow-menu fit than the interrupt button, not a worse one — extra friction before a
  destructive, rarely-needed action is a feature.

### Under-composer area — a natural next landing spot

**Landed** (transcript-restyle PR): everything below is implemented as designed. The header is now
back button + thread title only; the composer bar holds the combined status dot, model picker,
kebab menu (raw frames, shutdown), and interrupt.

Once the model picker/stop row exists under the composer, the operator's next observation: that
row is where several of the still-header-bound items above could consolidate, rather than a
separate settings surface:

- **Combined status bubble.** Replace the header's separate status `Badge` + harness `Badge`
  (`session.tsx:410-411`, `attached`/`connecting`/etc. plus `harness running`/`stopped`) with one
  dot/bubble summarizing both, hover/focus revealing the detail of each — the same
  `role="img"`/`aria-label`/`Tooltip` pattern this PR round already used for streaming/failed/sending
  (`StatusDot` in `session.tsx`). Two independent state machines (session attachment, harness
  process) folding into one glanceable indicator, with the breakdown one hover away.

  **Decided: four buckets, worst-axis-wins, first match in this order:**

  | #   | Condition                                                                        | Dot                        |
  | --- | -------------------------------------------------------------------------------- | -------------------------- |
  | 1   | `status` starts with `"runner: "`, or `harness === "lost"`                       | Error — red, static        |
  | 2   | `status` is `"connecting"`/`"reconnecting"`, or attached with `harness === null` | Pending — amber, breathing |
  | 3   | `status === "stream ended"`, or `harness === "stopped"`                          | Idle — gray, static        |
  | 4   | otherwise — attached and `harness === "running"`                                 | Healthy — green, static    |

  `status` is the free-form string `session.tsx`'s `EventSource` handlers set
  (`"connecting"`/`"attached"`/`"reconnecting"`/`"stream ended"`/`` `runner: ${detail}` ``);
  `state.harness` is `events.ts`'s typed `"running" | "stopped" | "lost" | null`. A plain function
  of the two, not a hand-maintained cross-product — easy to unit-test on its own. "Pending" reuses
  the same breathing animation `agentplane-breathing-dot` already gives the streaming dot, so the
  visual vocabulary for "still settling" stays one idiom across the app.

- **Hamburger/overflow menu near the composer**, holding the "Raw frames" switch and — per the
  header-row bullet above — the harness **shutdown** button (`IconPower`), plus room for the
  combined-status bubble's detail if it reads better as a menu item than a tooltip on small touch
  targets. A menu item reads by its text, not an icon alone, so "Shut down harness" would need its
  full label there (unlike today's icon-only `ActionIcon` with just a tooltip). **Opens upward**:
  the composer sits at the bottom of the viewport, so a Mantine `Menu` here needs its default
  Floating-UI flip behavior left alone rather than a hardcoded downward `position` — there's rarely
  room below the trigger.
- **Decided: interrupt (`IconPlayerStop`, stops the current turn) stays a visible icon, always —
  it does not move into the menu.** It's genuinely time-sensitive — the operator reaches for it
  _during_ an active turn, not at rest — so tucking it a menu-tap deep would trade away exactly the
  responsiveness the current always-visible icon gives it. Shutdown is the one that moves under the
  hamburger, per the header-row bullet above; interrupt is the one that doesn't.

## Badges → dots, and message role → layout

`session.tsx` badges, concretely:

- `InputView` (`session.tsx:59-71`): a "user" `Badge` plus a conditional "sending" badge, wrapping
  every user message in a bordered `Paper`.
- `ItemView` (`session.tsx:109-129`): a kind-label `Badge` (`assistant`/`tool`/etc, from
  `KIND_LABELS`), plus conditional "streaming"/"failed" badges, again each wrapped in its own
  `Paper`.
- `ItemGroupView`/run summaries (`session.tsx:152-154`) and `TurnHeader` (`session.tsx:182`) add
  more of the same pattern.

Two separable ideas from the operator, both plausible:

1. **Role → styling, not a badge.** Replace the "user" badge + bordered `Paper` with a chat-bubble
   convention instead: assistant content full-width (as it mostly is today), user input right-aligned
   in a bubble. This removes one `Badge` per message and the vertical rhythm of `Group` + `Text`
   inside every `Paper`, which is the biggest real-estate win since it applies to every turn.
2. **Ephemeral status badges → dots with tooltips.** "sending", "streaming", "failed" are
   transient/binary states — good candidates for a small colored dot (`role="img"` +
   `aria-label`/`title`, the pattern `haku-console`'s `settings_panel.tsx` already uses for its
   daemon-status dots) with the text available on hover/focus instead of always rendered. Static
   content labels (tool name, "reasoning") likely stay as text since they're not transient status.

Needs a real accessibility pass before landing (dots need a non-hover-only affordance for
keyboard/touch — the mantine `Tooltip` component plus a `title` fallback, not a mouseover-only cue).

## Actions approval surfacing — `actions.tsx`, `app.tsx`

Today `/actions` is a normal routed page (`app.tsx:101-102`); a pending approval is only visible if
the operator navigates there. Requested behavior: a pending Action decision should interrupt with a
modal/overlay over whatever page is currently open, not wait for a page visit — the approval loop
(`decision_pending` in `STATE_COLORS`, `actions.tsx:14`) is time-sensitive in a way most of the rest
of the app isn't.

Two sub-parts:

- **Global pending-approval overlay**: needs a top-level subscription (likely alongside wherever
  `live.tsx`'s push/live-update mechanism already lives) that can raise a modal from any route when
  a new `decision_pending` Action Request appears, not just from the `/actions` page's own fetch
  loop (`actions.tsx`'s current polling/fetch is scoped to that page).
- **Historical (decided) actions out of the main view**: once an Action is `allowed`/`denied`/
  terminal, it doesn't need the same prominence as a pending one. Move the historical list under a
  menu/collapsed section, so the primary Actions surface (or the new overlay) only has to show what
  actually needs a decision right now.

## Process proposal

For "how do we do this": don't implement straight from this list. The moves above (badge → bubble,
header → overflow menu, modal-over-any-page) are layout/interaction decisions that are much cheaper
to get wrong in a mock than in `session.tsx`'s already-dense component tree. Two reasonable paths,
not mutually exclusive:

1. **Mock the two highest-value screens first**: the session view (header + transcript + composer)
   and the global pending-approval overlay, at both a mobile and desktop width, before touching
   `session.tsx`. A rough mock settles the header's reduced button set, where the model
   picker/stop control land relative to the composer, and what the bubble-vs-badge transcript
   actually looks like once assistant/user are styled instead of labeled.
2. **Land the nav/settings consolidation first, separately from the transcript rework.** Moving
   Connections/MCP servers/Notifications behind an overflow menu in `app.tsx` is a small, contained,
   easily-reviewed change with no visual-design ambiguity — it doesn't need a mock and can ship
   independently of (and before) the harder transcript/composer/approval-overlay design work. This
   matches the repo's own small-PR guidance (`AGENTS.md` § Splitting Work Into PRs): split by
   change, ship the uncontested part now, argue about the rest separately.

On "Claude Design used to be a thing": there's a `design` skill available in this environment for
drafting a multi-artboard visual mock as a published Artifact (desktop + mobile artboards side by
side, editable afterward) — that's the right tool for path 1 above once this plan is prioritized.
It drafts the mock; it doesn't touch `x/agentplane` code.

## Open questions (not decided here)

- Overflow menu placement/pattern for both the top nav and the per-session raw-frames/shutdown
  controls — one shared "settings" affordance, or two separate menus at different scopes?
- Whether the model picker moves to the composer on all widths or only below a breakpoint.
- Exact dot/tooltip component and whether it's worth factoring out of `haku-console`'s
  `settings_panel.tsx` pattern into a shared place both frontends can use, or just re-implemented
  locally (`x/agentplane` and `haku/console` are separate frontends today — check before assuming
  shared component infra exists).
- Whether the pending-approval overlay preempts the operator's current page/input, or docks
  non-modally (a toast/banner) so it can't interrupt something like an in-progress composer edit.
