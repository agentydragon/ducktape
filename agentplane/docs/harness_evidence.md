# Native harness evidence

The Claude and Codex drivers are grounded in live captures against LiteLLM and scripted tests that
run Claude Code 2.1.252 and the runner-matched Codex app-server 0.157.0 against a loopback model
endpoint. The probe under
[`../capture/`](../capture/) preserves ordered native frames and model bodies outside Git; the
tests under [`../harness_tests/`](../harness_tests/) carry the committed behavioral contract.

## Observed harness behavior

- Claude uses newline-delimited stream/control JSON. With `CLAUDE_CODE_MAX_RETRIES` bounded, the
  pinned version retries a stream lost before or after visible text as a non-streaming request.
- Codex app-server uses newline-delimited JSON-RPC-shaped messages. The pinned version emits retry
  notices and eventually reports a failed turn after repeated upstream losses.
- A Claude active-turn second input is native input behavior; it is not steering unless native
  evidence supports that interpretation.
- Codex exposes `turn/steer` and `turn/interrupt`; `turn/steer` requires `expectedTurnId` and joins
  the running turn.
- On Codex 0.157.0, interrupting an in-flight streamed model request closes that upstream request
  before the process exits; the interrupted-turn recovery test waits for this close.
- Codex switches to code mode for model ids in its built-in catalog. Scripted tests use an unknown
  model id to exercise the classic function-call shape.
- Claude's session-title model call is suppressed by `--name`.
- Both harnesses run with thinking or reasoning enabled; tests assert that the thinking signature
  or encrypted reasoning is echoed back upstream.
- Idle resume uses Claude `--resume` or Codex `thread/resume`, never transcript replay or prompt
  redispatch.

These are observations from the currently pinned harnesses. The runner must not manufacture retry,
steering, acknowledgement, or successful-completion semantics that the native process did not emit.
An unrecognized native kind remains a named `Unknown*` value so captures can reveal protocol drift
without silently discarding it.

## Codex threads on one app-server

The harness test binary is `@openai/codex` 0.157.0 from `MODULE.bazel`; the corresponding upstream
`rust-v0.157.0` tag resolves to source commit
[`00c972ed`](https://github.com/openai/codex/tree/00c972ed5d6ff6499317fd41b7f23605b8e6850d).
At that revision, [`ThreadStartParams`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L62-L188)
creates a thread with its own `cwd`, model and developer instructions;
[`TurnStartParams`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L167-L284)
and [`TurnInterruptParams`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L327-L335)
address a `threadId`, while
[`TurnStartedNotification`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L532-L552)
returns it. The server keeps a set of thread IDs per connection in
[`ThreadStateManager`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/thread_state.rs#L343-L346)
and attaches each started thread to the requesting connection in
[`thread_start_task`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/thread_processor.rs#L1562-L1577).
[`turn_start_inner`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/turn_processor.rs#L522-L530)
loads the requested thread before submitting input, and
[`turn_interrupt_inner`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/turn_processor.rs#L1595-L1635)
loads and interrupts that thread. `thread/resume` by ID reloads a stopped thread and rejoins an
already running one, as specified in
[`ThreadResumeParams`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L335-L353).

`test_independent_threads_share_a_process_and_resume_by_thread_id` is the real-binary scenario for
two separately started, persisted threads through one `CodexRun` and one stdio process. It holds
both model requests open, checks each request's thread ID, model and developer instruction, runs
`pwd` in the second thread's workspace, interrupts the first thread, and completes the second. It
then starts a fresh process with the same `CODEX_HOME`, resumes both thread IDs over its one
connection, and completes a follow-up turn on each. Turn start/completion notifications and the
interrupt request are checked against their thread and turn IDs. These are independent root threads
created with `thread/start`, not native child tasks or read-only projections. The focused Bazel target
is the runtime gate for these assertions.

On a passing target, this scenario demonstrates concurrent turn routing and thread-scoped
cancellation for the pinned binary. The selected per-thread options are model, reasoning effort,
developer instructions and working directory; this does not establish isolation for every thread
option. Both threads share the process's `CODEX_HOME`, environment, configured provider and API key.
The protocol has no per-thread credential field, and this test does not compare authentication
identities. The app-server shuts down its managed threads together in
[`ThreadProcessor::shutdown_threads`](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/request_processors/thread_processor.rs#L1281-L1291),
while the test's cross-process resume is a graceful restart after both turns settle. Active
multi-thread crash recovery is not measured here.
The result characterizes the upstream protocol and does not select a runner process/session model.

## Native subagents

Opt-in scenarios script a real native delegation and settle every child model exchange: the
subagent cases in each harness's `test_tools.py`, `codex/test_v2_discovery.py`,
`claude/test_root_identity.py` and the
[RemoteIO spike](../harness_tests/x/claude_remote_io/README.md) at Claude Code 2.1.292. Model
requests identify children for the test only; the runner cannot see them.

Claude Code 2.1.252:

- `system/task_started` carries `task_id`, the originating `tool_use_id`, task type and background
  status; the parent `Agent` tool result exposes the same identity as `agentId`. The model-facing
  `Agent` tool is `Task` in the native init roster.
- Completed child prose and forwarded child tool frames carry `parent_tool_use_id` by default. Their
  `session_id` can be the parent's, so it does not identify a child.
- `system/task_notification` names the child and its outcome.
- `SendMessage` to a completed child resumes the same agent ID with its earlier answer in context; a
  later notification names the same child, and `TaskOutput` with `block=false` reads its result. A
  send receipt is not completion.
- After a clean parent exit and native resume, parent history keeps the child's result, but
  `TaskOutput` on the old child returns a missing-task error and no new `task_notification` arrives
  during resume, input or query.
- A fresh root's initialization frames carry no `session_id`, with or without `--session-id`, over
  a 250 ms observation. The first input's frames and result carry it, equal to the requested ID.

Claude Code 2.1.292, over both `stream-json` and RemoteIO:

- Task identity and child forwarding are as above.
- Native-file resume after a crash with an active child reports that task `stopped` before any new
  parent input; the notification can omit the original `tool_use_id`. An empty successful zero-turn
  result accompanies it and over stdio can precede the initialization control response. It settles
  no user command and does not mean initialization failed.
- After a completed child, crash or clean exit, parent history keeps the result and no new
  notification arrives. `TaskOutput` is absent from this configuration's roster; its
  `No such tool available` error is not a missing task.

Codex app-server 0.157.0 with `multi_agent_v1`:

- `collabAgentToolCall` items carry `senderThreadId` and, on a successful spawn,
  `receiverThreadIds`. A completed wait item reports the child's completion in `agentsStates`.
  Spawn-tool completion and child-work completion are separate observations.
- After a clean exit and resume, parent history keeps the result; `wait_agent` on the old child
  returns `not_found`, and `agentsStates` reports `notFound`.

Codex app-server 0.157.0 with `multi_agent_v2` (`collaboration` tools):

- A spawn emits `item/completed` with `subAgentActivity`, `kind=started`, `agentThreadId` and
  `agentPath`; the enclosing `params.threadId` is the parent. Child `agentMessage` output arrives
  under the child's thread ID, and the parent stream reports `subAgentActivity(kind=completed)`.
- While live, `thread/read` gives the child's parent and session tree, loaded enumeration lists it,
  and its `sessionId` equals the root's.
- After a clean exit or crash and root resume, loaded enumeration lists only the root and
  `thread/list(sourceKinds=["subAgent"])` is empty, but `thread/list(parentThreadId=...)` finds the
  child. `thread/read(includeTurns=true)` keeps its ID, `parentThreadId` and ancestry. A completed
  child keeps its answer and `completed` turn; an active child killed with its model response held
  reads back with an `interrupted` turn, null `completedAt` and no answer. Status is `notLoaded` in
  every case, which says nothing about fate. The unloaded read reports `sessionId` equal to the
  child's thread ID, so that field is no durable tree key.
- These reads send no `turn/start` and start no model request within a bounded observation.

Unobserved so far: live client reconnect, concurrent or nested children, independent child
control, and recovery without native files.

## Evidence contract

The scripted scenarios cover launch and handshake, streaming and terminal output, tool lifecycles
and workspace effects, active-turn input, interruption, upstream retry and exhaustion, follow-up
after transport failure, and idle native resume. Assertions synchronize on protocol checkpoints,
not sleeps, and check scenario-relevant markers rather than volatile packet equality.

A live probe records native stdin, stdout, stderr, model request bodies, streamed response chunks,
and controlled loss markers. It never records HTTP headers, cookies, credentials, OAuth state, or
private user data. File order is the ordering authority; do not add redundant hashes, lengths,
timestamps, parsed copies, or manifest inventories.

When a harness pin changes or a new behavior needs coverage:

1. run the live probe against a synthetic workspace and the real model endpoint;
2. compare the native and model exchanges with the scripted expectations;
3. update the driver and scripted scenario only for behavior Agentplane intends to support; and
4. update the harness pin.

Classify harness-specific results as **Proven**, **Supported differently**, **Unsupported**, or
**Environment-blocked** instead of normalizing them into a false common success.
