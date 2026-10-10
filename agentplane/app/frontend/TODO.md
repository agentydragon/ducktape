# TODO — Agentplane operator frontend

Gaps to close if a workflow needs them; none is committed.

## Thread tool schemas generated from their implementations

The thread's shell tool calls have the same hazard: `threads/command_calls.ts` mirrors Claude Code's
`Bash` input and the arguments `runner/codex.py` records for `commandExecution` by hand, strictly, so
a field a harness adds sends every such call to the JSON view while the tests still pass.

## Thread view UX

- **Give block Markdown clear one-line summaries**: `Markdown`'s `singleLine` mode makes lists,
  tables, and blockquotes inline, but removes list markers and flattens table cells without visible
  separators; a blockquote also loses its quote cue. Choose compact separators and markers that keep
  these structures readable in reasoning previews, while leaving the expanded Markdown unchanged.
- **Reasoning disclosure toggle with nothing behind it**: the reasoning branch of `EntityCard`'s body
  (`threads/thread_cards.tsx`) wraps a reasoning item's text in `LazyBody`'s `RetainedDisclosure`
  (`threads/retained_disclosures.tsx`) whose body is shown only once expanded, though the window reads it
  ahead -- whenever `entity.textRef` is non-null. A reasoning item can still resolve to empty text once that payload loads, and by then the toggle
  has already invited a click for nothing. Unlike the `textRef === null` case just below it (plain dimmed
  "Reasoning" text, no toggle at all), there's no cheap signal to suppress the toggle before the fetch
  resolves; worth figuring out one (e.g. from the fold/view layer) rather than always rendering it optimistically.
- **Fold adjacent reasoning items inside mixed tool/reasoning runs**: `historyRows` groups consecutive tool calls and
  reasoning items together, and opening a run currently shows each item separately. When reasoning items are
  adjacent within a mixed run, fold each consecutive reasoning group into a nested disclosure whose collapsed line
  joins their text and whose expanded body shows the original individual items in order.
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
