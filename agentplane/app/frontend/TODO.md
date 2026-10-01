# TODO — Agentplane operator frontend

Gaps to close if a workflow needs them; none is committed.

## Per-Action widget schemas generated from the servers' models

The SSH `exec` widgets' zod schemas (`actions/rendering/ssh.tsx`) and the stored result in
`visual/harness.tsx` mirror `x/ssh_mcp_server/server.py`'s `exec` signature and `ExecResult` by
hand. The schemas are strict, so a field the server adds sends real results to the generic view
while every test still passes. Consider generating them from the pydantic models, for example from
the JSON Schema FastMCP publishes as the tool's `inputSchema` and `outputSchema`, converted to zod at
build time.

## Thread view UX

- **Per-message evidence icon is always-on visual noise**: `EvidenceToggle` (`threads/thread_evidence.tsx`, the
  magnifying-glass `IconZoomCode` button) renders unconditionally at every one of its 7 call sites, one per
  message/entity, whether or not a reader is looking at that row. Consider a per-message overflow affordance
  instead -- e.g. a vertical-dots button, shown only on hover (desktop) or tap (mobile), holding this and other
  message-level debug actions. This would be its own menu, separate from the thread's topbar menu (`topbar.tsx`'s
  `TopbarActions`, holding "Debug history" / "Shut down harness" / thread id) -- a per-message menu and a
  per-thread menu, not one merged control, even though both would share the dots-icon pattern.
- **Reasoning disclosure toggle with nothing behind it**: the reasoning branch of `EntityCard`'s body
  (`threads/thread_cards.tsx`) wraps a reasoning item's text in `LazyBody`'s `RetainedDisclosure` -- a
  `<details>` (`threads/retained_disclosures.tsx`) whose payload isn't fetched until expanded -- whenever `entity.textRef`
  is non-null. A reasoning item can still resolve to empty text once that payload loads, and by then the toggle
  has already invited a click for nothing. Unlike the `textRef === null` case just below it (plain dimmed
  "Reasoning" text, no toggle at all), there's no cheap signal to suppress the toggle before the lazy fetch
  resolves; worth figuring out one (e.g. from the fold/view layer) rather than always rendering it optimistically.
- **Collapsing a long expanded block requires scrolling back up to its toggle**: `RetainedDisclosure`
  (`threads/retained_disclosures.tsx`) is a plain `<details>`/`<summary>` -- opening a long one (`LazyBody`'s
  Reasoning/Arguments/Output, or `CollapsibleRows`'s "N tool call(s), N reasoning step(s)" run/lifecycle wrapper,
  both in `threads/projected_session.tsx`) and scrolling down through its content scrolls the `<summary>` that collapses
  it off the top of the screen, so collapsing means scrolling back up first -- and a long enough run (many tool
  calls and reasoning steps spanning several screens) makes this worse, not just more of the same, since the
  toggle can be scrolled arbitrarily far out of reach. One direction: keep the summary/toggle stuck to the
  viewport top while its content is still in view, only releasing it once scrolled fully past -- would need a
  custom sticky-summary treatment rather than the native `<details>` element as is. A second idea, instead of or
  alongside a sticky summary: draw a continuous vertical rail down the open block's left edge, clickable anywhere
  along its length to collapse -- reachable from wherever the reader has scrolled to, without depending on any
  one row staying pinned.
- **Consider squashing the "observation" naming layer**: not remembered as a deliberate design choice. `Event`
  (`protocol/event.proto`)'s payload is a `oneof` field literally named `observation`; that name then propagated
  outward into `runner/observation.py`'s `Observation` type, the archive/API layer
  (`app/agent_runtime/events/event_log.py`'s `observations()`/`observation_entry()`, `api.py`'s
  `/observations/{cursor}` and `/evidence/{observation_cursor}/frames` routes), and the debug UI
  (`threads/chronological_debug.tsx`'s "Observation N raw frames"). Worth revisiting whether this is a distinction worth
  keeping or whether it should just say "Event" everywhere a stored `Event` is meant.
- **Bubble chrome and the user bubble's blue read as unnecessary decoration**: `.agentplane-user-bubble`
  (`threads/projected_session.css` ~line 40) fills the operator's bubble with `var(--mantine-color-blue-light)`; feedback
  was grey would do, since role already reads from position (right-aligned) without needing a hue. More broadly,
  consider dropping bubble/card chrome across `EntityCard` altogether -- the user bubble's background, and the
  bordered `Paper` around tool calls and reasoning (`threads/projected_session.tsx` ~line 423) -- and distinguishing rows
  by their text and a light shade of grey instead, reserving actual color for when it's semantically meaningful
  (as the prominent-lifecycle `Alert color="red"` at ~line 376 and the failed-tool-call `Badge color="red"` at
  ~line 444 already do).

## Approval-arrival attention, and merging the pending/history Action pages

A pending decision (`ActionRequests`, `actions/requests.tsx`, `/actions`) currently only shows up if
the operator is already on the Actions page -- nothing calls attention to a new one arriving while
working elsewhere, e.g. inside a thread. `haku/console` has already solved a closely related
problem, worth drawing on rather than reinventing:

- Its approvals surface is a **non-modal drawer** (`haku/console/frontend/shell_chrome.tsx`),
  triggered from a rail button present on every page, floating over whatever page is showing rather
  than being its own route -- driven by a live WebSocket (`console_events.ts`) that invalidates
  panels across every open tab without a reload.
- For when no console tab is even open, it separately uses **Web Push**
  (`haku/console/notifications/push.py`/`push_routes.py` + the service worker
  `frontend/sw.ts`): one versioned OS notification per queued call, with Approve/Deny actions in the
  notification itself (calling the ordinary exact-Origin decision endpoint under the operator's
  session), deep-linking into a small chrome-free approval window (`approvals_embed_page.tsx`)
  rather than the full console.

Agentplane's `ActionRequests` already has the live signal this would build on (`/actions/stream`,
consumed by `useActionRequests` in `requests.tsx`) -- what's missing is anything that grabs attention
outside the Actions page itself. Whatever surfaces this must not navigate the operator away from a
thread they're reading (mirroring haku-console's drawer-over-content pattern, not a route change),
and needs to read well on both mobile and desktop.

**Tension worth resolving deliberately, not by copying haku-console verbatim**: haku-console does
_not_ actually merge pending and history into one page -- pending decisions live in the cross-page
drawer, while decided calls stay on their own separate full page (`frontend/tool_calls_page.tsx`,
its own README section "Past tool calls -- full-page history"). Merging agentplane's
`ActionRequests` (`requests.tsx`) and `ActionHistory` (`history.tsx`) into one page, pending-then-
past, is a different shape than that. The two already share one data source (`useActionRequests` in
`requests.tsx` -- one hook both components call, then filter by `request.state`), so the merge
itself would be a straightforward rendering/routing change -- the open question is whether merging
them into one page is still the right shape once pending decisions also get a cross-page drawer, or
whether the drawer supersedes the need for a merged page.

Needs more design thinking and probably mocks before implementing.

## Hidden characters in what the operator approves

Bidi controls, zero-width and other default-ignorable characters, and control characters render
invisibly, so a command or argument can read differently from what runs. highlight.js escapes only
markup characters and `JSON.stringify` only C0 controls, so neither the highlighted views
(`syntax_highlight.tsx`) nor plain text (titles, descriptions, stdout, drawn arguments) show them.
One way: in `highlight()`, between highlight.js and DOMPurify, wrap each
`[\p{Bidi_Control}\p{Default_Ignorable_Code_Point}]` match and each control character other than tab
and newline in a span showing its code point; do the same in plain text through a small React helper;
and warn on the card, as GitHub does for bidi text. Bidi controls and tag characters warrant a loud
marker; emoji joiners and variation selectors a quiet one.
