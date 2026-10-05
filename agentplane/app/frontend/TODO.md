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

- **Reasoning disclosure toggle with nothing behind it**: the reasoning branch of `EntityCard`'s body
  (`threads/thread_cards.tsx`) wraps a reasoning item's text in `LazyBody`'s `RetainedDisclosure` -- a
  `<details>` (`threads/retained_disclosures.tsx`) whose body is shown only once expanded, though the window reads it
  ahead -- whenever `entity.textRef` is non-null. A reasoning item can still resolve to empty text once that payload loads, and by then the toggle
  has already invited a click for nothing. Unlike the `textRef === null` case just below it (plain dimmed
  "Reasoning" text, no toggle at all), there's no cheap signal to suppress the toggle before the fetch
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

## Durable local storage for thread windows

`RetainedThreads` (`threads/thread_store.tsx`) keeps the last few left threads' rows, complete bodies and log
positions in memory, so it covers switching threads but not a reload or a second tab, which still read every
row and body again. Consider persisting them in IndexedDB, keyed by thread, projection epoch, owner and
generation, so a reload only catches up from the saved log position. It needs a size budget with eviction,
invalidation when the epoch changes, and a decision on whether the proxy's per-user authorization allows
keeping thread content at rest in the browser.

## Bound reading thread bodies ahead by size

A window reads the bodies of every row it holds (`PayloadShape.readAhead` in `threads/thread_store.tsx`), but a
payload reference carries `chunk_count` and no byte size, so the only bound on a read ahead is 20 bodies per
read. A large tool output is read, and kept in memory with its thread's retained window, whether or not it is
ever opened. Consider putting `content_bytes`, which the payload manifest already stores, on the reference so the
client can leave bodies past a size to be read when shown, and a memory budget across the retained threads.
