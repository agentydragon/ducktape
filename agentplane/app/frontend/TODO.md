# TODO — Agentplane operator frontend

Gaps to close if a workflow needs them; none is committed.

## Per-Action widget schemas generated from the servers' models

The SSH `exec` widgets' zod schemas (`actions/rendering/ssh.tsx`) and the stored result in
`visual/harness.tsx` mirror `x/ssh_mcp_server/server.py`'s `exec` signature and `ExecResult` by
hand. The schemas are strict, so a field the server adds sends real results to the generic view
while every test still passes. Consider generating them from the pydantic models, for example from
the JSON Schema FastMCP publishes as the tool's `inputSchema` and `outputSchema`, converted to zod at
build time.

The thread's shell tool calls have the same hazard: `threads/command_calls.ts` mirrors Claude Code's
`Bash` input and the arguments `runner/codex.py` records for `commandExecution` by hand, strictly, so
a field a harness adds sends every such call to the JSON view while the tests still pass.

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
  (`threads/retained_disclosures.tsx`) is a plain `<details>`/`<summary>` -- opening a long one (a tool call or
  reasoning step's `StepLine`, or `CollapsibleRows`'s "N tool call(s), N reasoning step(s)" run/lifecycle wrapper
  in `threads/projected_session.tsx`) and scrolling down through its content scrolls the `<summary>` that collapses
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
  (`app/threads/events/event_log.py`'s `observations()`/`observation_entry()`, `api.py`'s
  `/observations/{cursor}` and `/evidence/{observation_cursor}/frames` routes), and the debug UI
  (`threads/chronological_debug.tsx`'s "Observation N raw frames"). Worth revisiting whether this is a distinction worth
  keeping or whether it should just say "Event" everywhere a stored `Event` is meant.
- **Bubble chrome and the user bubble's blue read as unnecessary decoration**: `.agentplane-user-bubble`
  (`threads/projected_session.css` ~line 40) fills the operator's bubble with `var(--mantine-color-blue-light)`; feedback
  was grey would do, since role already reads from position (right-aligned) without needing a hue. More broadly,
  consider dropping bubble/card chrome across `EntityCard` altogether -- the user bubble's background, and the
  bordered card an opened tool call or reasoning step gets (`CollapsibleCard` in `threads/thread_cards.tsx`) -- and distinguishing rows
  by their text and a light shade of grey instead, reserving actual color for when it's semantically meaningful
  (as the prominent-lifecycle `Alert color="red"` at ~line 376 and the failed-tool-call `Badge color="red"` at
  ~line 444 already do).

## Hidden characters in ordinary Action text

The shared CodeMirror viewer now marks bidi controls, zero-width/default-ignorable characters,
control characters, and Unicode line separators in code-shaped Action arguments, shell commands,
results, and fenced Markdown. It keeps the source text intact and distinguishes bidi/control markers
from quieter formatting markers. Ordinary Action titles and descriptions still render as plain text, and so do a
thread tool call's one-line summaries other than a shell command's (the model's description, a
tool's JSON); decide whether they also need inline markers or an approval-card warning.
