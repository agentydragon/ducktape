# Common runner protocol

Status: **target hard-cut seam.** The current wire remains documented by the runner
specification until that cutover lands; this page does not add a compatibility mapping
between the old and target command shapes.

The runner is the shared seam over Claude Code and Codex. Exact wire fields, replay,
and journal recovery are in [the runner specification](../runner/SPEC.md). The
cross-layer identity, command, projection, and UI contract is
[Thread, runner, and harness layering](thread_layering.md). That document is the
authoritative definition of Thread command versus runner Command, app-versus-runner
receipt, replica ownership, and Raw-mode ordering.

## What the runner seam owns

- One bidirectional Attach stream per runner session over Open, Command, and Detach,
  plus replayable sequenced Events.
- A durable command-journal receipt before CommandReceived and causal effect/outcome
  Events afterwards. It does not call an unsupported native operation successful.
- Native harness frames delivered verbatim as Native Events, with derived Events citing
  their source sequences.
- Only common behavior proved by the scripted Claude/Codex harness tests. A caller
  selects a harness through SessionSpec.harness, not with adapter-specific operations.

The current commands are `SubmitInput`, `ChangeModel`, `InterruptTurn`, and
`StopRunnerSession`. `SubmitInput` joins a running turn; it is not a common steer or
queue abstraction. The runner reconciles uncompleted commands after restart and
never emits an indeterminate outcome.

The runner is internal. It neither names product Threads nor owns the browser API,
PostgreSQL outbox, Kubernetes Sandbox lifecycle, authorization, or presentation.

## Harness differences remain evidence

Claude and Codex can have different native control/lifecycle frames, retry behavior,
input queues, and model-change mechanisms. Common runner Events record only the
meaning both adapters can prove; native frames keep the underlying difference
inspectable. Similar native operations are not evidence of identical semantics.

In particular, input coalescing/queueing, steering, interruption, resume, and runtime
model control remain adapter responsibilities. The native constraints and evidence are
documented alongside the relevant adapter tests and captures, including
[Claude input queue behavior](claude_input_queue.md). Any future common enqueue,
withdraw, or capability surface must preserve the demonstrated asymmetry rather than
make an app-side generic queue.

## Capture evidence contract

A live capture preserves complete native frames in both directions, framing/file order,
native request/session/thread/turn/item/tool ids, model request bodies and streamed
chunks, and enough process-exit evidence to diagnose a failed run. It is investigation
evidence outside Git; behavioral assertions belong in scripted tests. Refreshing pinned
harness evidence is described in [the harness tests README](../harness_tests/README.md).
