# Common runner protocol

The shared generated messages are `Command`, `Event`, `EventEntry`, and `Follow`.
The runner and app reuse them for command delivery and retained Event replay.

The runner is the shared seam over Claude Code and Codex. Exact wire fields, replay,
and journal recovery are in [the runner specification](../runner/SPEC.md). The
cross-layer identity, command, projection, and UI contract is
[Thread, runner, and harness layering](thread_layering.md). That document is the
authoritative design for Thread identity, queue placement, durability boundaries,
replica ownership, and Raw-mode ordering. An app queue is an open product choice.

## What the runner seam owns

- Bidirectional Attach streams over Open, Command, and Detach, plus replayable
  sequenced Events. Multiple attachments can follow and command the same session.
- Durable command-journal admission represented by `CommandAdmitted`, before causal effect/outcome
  Events afterwards. It does not call an unsupported native operation successful.
- Native harness frames delivered verbatim as Native Events, with derived Events citing
  their source sequences.
- Only common behavior proved by the scripted Claude/Codex harness tests. A caller
  selects a harness through SessionSpec.harness, not with adapter-specific operations.

The current commands are `SubmitInput`, `ChangeModel`, `InterruptTurn`, and
`StopRunnerSession`. `SubmitInput` joins a running turn; it is not a common steer or
queue abstraction. The runner reconciles uncompleted commands after restart. Native
execution before durable outcome evidence needs its own recovery proof; journal
deduplication alone does not guarantee exactly-once native execution.

The runner is internal. The Thread identity design gives it a stable journal key;
it does not own the browser API, PostgreSQL, Kubernetes lifecycle, authorization,
or presentation.

## Harness differences remain evidence

Claude and Codex can have different native control/lifecycle frames, retry behavior,
input queues, and model-change mechanisms. Common runner Events record only the
meaning both adapters can prove; native frames keep the underlying difference
inspectable. Similar native operations are not evidence of identical semantics.

In particular, input coalescing/queueing, steering, interruption, resume, and runtime
model control remain adapter responsibilities. The native constraints and evidence are
documented alongside the relevant adapter tests and captures, including
[Claude input queue behavior](claude_input_queue.md). Any future common enqueue,
withdraw, or capability surface must preserve the demonstrated asymmetry. Queue
placement in the app does not change native execution semantics.

## Capture evidence contract

A live capture preserves complete native frames in both directions, framing/file order,
native request/session/thread/turn/item/tool ids, model request bodies and streamed
chunks, and enough process-exit evidence to diagnose a failed run. It is investigation
evidence outside Git; behavioral assertions belong in scripted tests. Refreshing pinned
harness evidence is described in [the harness tests README](../harness_tests/README.md).
