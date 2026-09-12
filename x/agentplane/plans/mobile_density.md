# Mobile/compact UI density

Status: **captured, not designed.** Nothing below is a committed direction yet — this is the
operator's raw list plus the concrete current-code anchors for each item, so a later pass can pick
items up without re-deriving where they live. No implementation until this gets a real design pass.

The session header/transcript/composer piece of this plan (badges → dots, role → bubble styling,
model picker + composer bar consolidation) landed via the transcript-restyle PR. What's left:

## Top-level nav — `app.tsx`

`AppRoutes` renders one `Group` of five always-visible buttons: Sandboxes, Actions, Connections,
MCP servers, Notifications (`app.tsx:70-98`). Proposal to evaluate: keep Sandboxes and Actions as
primary (they're the operational loop — see below on Actions specifically), move Connections, MCP
servers, and Notifications under a settings/overflow menu. That alone removes 3 of 5 buttons from
the permanent top row.

Whether "Sandboxes" stays the primary landing button at all is exactly the question in
[session-first navigation](session_first_navigation.md) — a bigger, separate redesign; don't block
this smaller nav cleanup on that decision.

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

For "how do we do this": don't implement straight from this list. A pending-approval overlay
preempting arbitrary pages is a layout/interaction decision much cheaper to get wrong in a mock
than in the app's already-dense component tree — mock it (and the nav overflow menu) at both a
mobile and desktop width before touching code, the way the transcript-restyle PR's own composer-bar
mock did. There's a `design` skill available in this environment for drafting a multi-artboard
visual mock as a published Artifact; a single-artboard HTML mock (as an Artifact, following the
`artifact-design` skill's guidance) works just as well for a narrower one-screen question.

## Open questions (not decided here)

- Overflow menu placement/pattern for the top nav — its own affordance, or shared with wherever
  Settings/account-level controls end up living?
- Whether the pending-approval overlay preempts the operator's current page/input, or docks
  non-modally (a toast/banner) so it can't interrupt something like an in-progress composer edit.
