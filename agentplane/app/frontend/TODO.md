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

- **Chronological-debug link renders full width**: `ChronologicalDebugLink` (`chronological_debug.tsx`) is a compact
  Mantine `Button`, but the evidence page view in `projected_session.tsx` places it inside a `Stack`, whose default
  `align="stretch"` stretches every child to the container's width -- the button ends up as a full-width bar
  instead of a small pill.
- **Thread id shown redundantly once a thread has a title**: `ThreadTitle` (`thread_title.tsx`) uses the thread id
  both as the title field's placeholder when unset and as an always-visible dimmed caption next to the title once
  one is set. Once a title exists, the id line adds clutter for no benefit; it should move somewhere less
  prominent instead -- the composer's overflow menu (`Menu.Dropdown` in `projected_session.tsx`, currently "Debug
  history" / "Shut down harness") is one candidate.
- **Mobile topbar's only content is an inert hamburger**: `.agentplane-mobile-topbar` (`app.tsx`, `shell.css`) shows
  below 560px width and holds only the `IconMenu2` button that opens the sidebar -- nothing else fills that bar's
  width. Consider moving `ThreadTitle` (`thread_title.tsx`) into it on mobile, since `ProjectedSession` currently
  renders its own title/id row inline in the thread page rather than sharing the shell's topbar.
- **Per-message evidence icon is always-on visual noise**: `EvidenceToggle` (`projected_session.tsx`, the
  magnifying-glass `IconZoomCode` button) renders unconditionally at every one of its 7 call sites, one per
  message/entity, whether or not a reader is looking at that row. Consider a per-message overflow affordance
  instead -- e.g. a vertical-dots button, shown only on hover (desktop) or tap (mobile), holding this and other
  message-level debug actions -- mirroring the composer's existing `Menu` + `IconDotsVertical` pattern
  (`projected_session.tsx`, "Debug history" / "Shut down harness").
- **A pending sent message shows in its own box below the thread, not inline as a message**: a `submitInput`
  command still `outcome: "pending"` renders in the "Pending commands" region (`projected_session.tsx`'s
  `hasPendingCommands` `Stack`, ~line 1326) as a bordered `Paper` with a "Saved · awaiting effect" caption, separate
  from the conversation history -- rather than where the confirmed message will eventually land, styled like the
  `confirmed_input` bubble (`.agentplane-user-bubble`, same file ~line 349). Consider rendering it inline in the
  history instead, using that same bubble style but visually marked pending (italic, reduced opacity, or similar).
- **Reasoning disclosure toggle with nothing behind it**: the reasoning branch of `EntityCard`'s body
  (`projected_session.tsx` ~line 404) wraps a reasoning item's text in `LazyBody`'s `RetainedDisclosure` -- a
  `<details>` (`retained_disclosures.tsx`) whose payload isn't fetched until expanded -- whenever `entity.textRef`
  is non-null. A reasoning item can still resolve to empty text once that payload loads, and by then the toggle
  has already invited a click for nothing. Unlike the `textRef === null` case just below it (plain dimmed
  "Reasoning" text, no toggle at all), there's no cheap signal to suppress the toggle before the lazy fetch
  resolves; worth figuring out one (e.g. from the fold/view layer) rather than always rendering it optimistically.
- **Collapsing a long expanded block requires scrolling back up to its toggle**: `RetainedDisclosure`
  (`retained_disclosures.tsx`) is a plain `<details>`/`<summary>` -- opening a long one (`LazyBody`'s
  Reasoning/Arguments/Output, or `CollapsibleRows`'s "N tool call(s), N reasoning step(s)" run/lifecycle wrapper,
  both in `projected_session.tsx`) and scrolling down through its content scrolls the `<summary>` that collapses
  it off the top of the screen, so collapsing means scrolling back up first. One direction: keep the
  summary/toggle stuck to the viewport top while its content is still in view, only releasing it once scrolled
  fully past -- would need a custom sticky-summary treatment rather than the native `<details>` element as is.
- **Row spacing feels too loose, especially around collapsed blocks**: every virtualized history row gets a flat
  `paddingBottom: 8` (`projected_session.tsx` ~line 1141), uniform across row types -- text messages, collapsed
  `CollapsibleRows` tool-call/reasoning runs, everything. Feedback was that the gap between text and a collapsed
  tool block specifically could be roughly half what it is now. Since the padding is currently one constant for
  every row regardless of neighbor type, halving it flat would tighten all row gaps equally; making it tighter
  only around collapsed blocks specifically would need type-aware spacing instead -- worth deciding which.
- **Consider squashing the "observation" naming layer**: not remembered as a deliberate design choice. `Event`
  (`protocol/event.proto`)'s payload is a `oneof` field literally named `observation`; that name then propagated
  outward into `runner/observation.py`'s `Observation` type, the archive/API layer
  (`app/agent_runtime/events/event_log.py`'s `observations()`/`observation_entry()`, `api.py`'s
  `/observations/{cursor}` and `/evidence/{observation_cursor}/frames` routes), and the debug UI
  (`chronological_debug.tsx`'s "Observation N raw frames"). Worth revisiting whether this is a distinction worth
  keeping or whether it should just say "Event" everywhere a stored `Event` is meant.

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
