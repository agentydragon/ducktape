# Native child Session evidence

These are characterization tests against the repository's pinned binaries, not an Agentplane
Session implementation. They use real harnesses and scripted loopback model responses, with no
inference credentials or recorded conversations. Child creation is proved by a child model request
and native identity evidence, not by parent prose.

## Product boundary

The characterization landed in [#10776](https://github.com/agentydragon/ducktape/pull/10776).
On 2026-10-16 the operator selected enabling native delegation without exposing child Sessions.
The evidence establishes usable native execution and observation, not automatic Agentplane child
identity, authorization, transcript reconciliation or independent control. Discovery therefore does
not by itself remove the remaining product/authority decisions for child Session projection.

The Harness Host's [root-only normalization contract](../../env/harnesshost/docs/session_protocol.md#native-subagents) retains
raw child frames and parent delegation results without letting children mutate root turn/input state.
Child Session discovery remains deferred; its unmeasured configurations and control experiments
are not additional acceptance gates for native delegation.

## Claude stream-json (2.1.252)

The child scenarios in [`test_tools.py`](../claude/test_tools.py) establish:

- Two native children can remain active after the parent's initial result. Releasing the second
  child's model exchange first reverses completion order without exchanging their identities.
  `task_started.task_id` identifies the task; `tool_use_id` links its originating `Agent` call.
- Child thinking blocks, completed prose blocks, shell calls and shell results are forwarded with
  `parent_tool_use_id`. Their `session_id` is the **root's**, not a child Session identifier.
  The nested scenario correlates the grandchild's shell to the immediate parent's `Agent` call,
  and that call to the root's call. Both tasks have distinct native IDs.
- Holding a child response after its model text delta produces no attributed `stream_event` in
  the bounded observation. Completed blocks and tool results arrive while the child is still
  running; the complete trace also contains no child `stream_event`. This holds with the default
  CLI arguments and with `--forward-subagent-text` explicitly enabled. The tested CLI rejects
  `--no-forward-subagent-text` and `--forward-subagent-text=false`; this is not a claim about every
  possible SDK configuration route.
- A shell exit of 23 is an error tool result, not a failed child: the child can answer normally and
  produce `task_notification.status=completed`. Exhausted child model retries instead produce a
  `failed` notification with the same task/call linkage; the parent can continue successfully.
- Completed child transcripts are separate `subagents/agent-<task_id>.jsonl` files, containing the
  original assistant message IDs. Parent history retains the child completion notifications after
  restart, but the resumed native stream does **not** replay the child transcript or rediscover the
  tasks. After an active-child crash, the tested resume/input sequence emits no new child terminal
  notification and starts no child model request. Do not turn that absence into a completion event.

The completed-transcript crash scenario waits for the child journals and parent acknowledgement
to reach disk before killing the process. A result frame is **not** a persistence barrier: initial
captures exposed asynchronous journal writes. This test establishes recovery of persisted history,
not a zero-loss guarantee for crashes immediately after a result receipt. Parent model history is
also not a substitute for a child's complete transcript.

## Codex app-server (0.157.0)

[`test_v2_discovery.py`](../codex/test_v2_discovery.py) includes separate v1 and v2 cases:

- New children automatically stream their own text deltas, reasoning items, shell items/results
  and completion on the already initialized connection. The text assertion runs while the mock
  response is held open, before its tool call or final response has been released.
- V1 discovers children through `collabAgentToolCall` sender/receiver IDs; v2 uses
  `subAgentActivity.agentThreadId` and `agentPath`. `thread/read.parentThreadId` corroborates both.
  Reading IDs from model request metadata drives the script but is not the runner's discovery API.
- Two live siblings finish in reverse order, with independently attributed items. A failing shell
  does not fail the child turn. Explicit `thread/resume` of an already loaded child is compared
  with parent-only observation; it does not duplicate completed items.
- After a process crash, read-only `thread/read` recovers completed child transcripts with the
  same item IDs and ordering as their native completed-item streams. Reads do not load the children
  or start model requests. `notLoaded` is residency, not terminal outcome.
- V1 supports a grandchild with `agents.max_depth=2`, preserving immediate-parent ancestry after
  restart. Its leaf no longer offers delegation. Under the tested v2 `fork_turns=none` configuration,
  the first child already omits collaboration tools, even with that V1 depth setting. Other v2
  model/role/context-fork configurations are not characterized by this test.
- Exhausted model retries produce child-attributed retry errors and a failed terminal turn while
  ancestors remain active. Recovery distinguishes that failed leaf from ancestors/children whose
  active turns were interrupted by the process crash.

The automatic initial attachment also has a pinned-source explanation: app-server's
[`thread_created_rx` handler](https://github.com/openai/codex/blob/00c972ed5d6ff6499317fd41b7f23605b8e6850d/codex-rs/app-server/src/lib.rs#L1268)
attaches initialized connections to newly created threads. The behavior tests, not that source
inspection, are the acceptance criterion.

## Recovery boundaries

[`test_child_reconnect.py`](../codex/test_child_reconnect.py) exercises a real Codex WebSocket
disconnect without restarting app-server, separately for v1 and v2. The child remains active,
emits text and runs a shell tool while no client is connected. The new connection reads those
missing items with their stable IDs. Resuming only the root does not subscribe it to the existing
child's live deltas; explicitly resuming the loaded child does. The snapshot and subsequent live
items join without duplicate completed-item IDs in the tested sequence, and attachment/read
operations start no extra model request. This differs from automatic attachment when a child is
created on an already initialized connection.

Process restart and transport reconnect are different scenarios. Claude's current runner transport
is stdio; these tests do not claim a reconnectable stream-json transport. The separate
[RemoteIO experiment](../claude/x/remote_io/README.md) uses a different Claude pin and must not be
treated as evidence for 2.1.252.

Child input, interrupt and independent execution resume are intentionally outside this work.
Read/attachment operations are used only to characterize visibility and recovery, not to initiate
new child work. Parent interruption, arbitrary crash timing, every alternative role/model setting,
and a production snapshot/live-stream reconciliation algorithm remain outside these assertions.

Run the affected scenarios with:

```sh
bbr test //agentplane/native/claude:test_tools \
  //agentplane/native/codex:test_v2_discovery \
  //agentplane/native/codex:test_child_reconnect
```

Native stdio and WebSocket traces are Bazel undeclared test outputs. Negative visibility assertions
are bounded observations of the named interface/configuration, not claims that no other interface
can expose the information.
