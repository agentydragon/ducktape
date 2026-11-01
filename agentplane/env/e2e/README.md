# Environment end-to-end tests

This package exercises complete Sandbox environment topologies with real pinned Linux harnesses,
container process boundaries, and VM supervision. Production contracts live with their owners:
[Environment Runtime](../runtime/SPEC.md), [Harness Host](../runtime/harnesshost/SPEC.md), and
[Execution Service](../runtime/execution/SPEC.md). Test helpers live in [`testing/`](testing/), and this
package defaults to test-only in Bazel.

## Tests

`//agentplane/env/runtime/harnesshost:harnesshost_test` and `//agentplane/env/runtime/harnesshost/codex:codex_test` run with Go's race detector. The
Codex router test drives parallel sessions through shared host, request, and thread routing state. The
journal suite (`//agentplane/env/runtime/harnesshost/journal:journal_test`) also runs with the race detector;
the runner suite covers process and Session integration. The VM fixture runs the separate
pure-Go `//agentplane/env/runtime/supervision:environment_runtime_systemd_fixture` test binary against its real systemd manager before starting
Runner. It exercises direct argv/stream semantics, lost launch replies, Runner death, stop status,
reaper retries and scoped startup fencing. Ordinary VM Session setup checks workspace creation,
credentials, resource mapping and detached descendants; VM tools cover execution and OOM.
The production `//agentplane/env/runtime/cmd:agentplane-environment-runtime` is pure Go.

### Native subagents

Both launches enable native delegation: Codex multi-agent tools and Claude's `Agent`,
`SendMessage`, and `TaskOutput`. Children remain native harness conversations, not additional
Agentplane Sessions. Discovery, attachment, independent child controls, and child transcript UI
are not provided.

All received native frames remain in the Session's raw event history. Normalized transcript and
turn state belong only to the root conversation: Codex notifications are scoped by root thread ID;
Claude child-attributed frames and task lifecycle notices are raw-only. Control requests and RPC
responses still receive normal handling, without child output acknowledging pending root inputs.
Root delegation calls and results remain visible, including Codex child-state summaries.

Codex v2 `subAgentActivity` maps to an existing tool item. Its successful result means the reported
activity was observed (including an interruption), not that the child succeeded. The result retains
the native activity kind and child identity; unrecognized future activity kinds remain raw-only.

`test_subagents.py` drives both real harnesses with mock model responses, checking background-child
isolation while the parent is idle (Claude) or active (Codex), with only one Agentplane Session.

### Test infrastructure

Each test is one interaction script run against both harnesses, written against
`testing/driver.py`: it plays the Runner Gateway's side of the control stream and reads each
session's Events from the stand-in ingress in `testing/gateway.py`;
the parametrized `model` fixture is the only place that knows the model API dialect. The scripted
model is shared with other packages' tests in [`agentplane/testing/`](../testing):
`scripted_model.py` is the neutral vocabulary (`Text`, `Reasoning`, `ShellCall`, and the request
markers), and `claude_model.py` and `codex_model.py` speak the two dialects. Harness fixtures live
in `testing/`, where `launches.py` wires the pinned binaries to a scripted upstream. `test_restart.py`
runs the runner as its own process so a crash takes its harnesses with it. No test here runs the
image (<image.nix>), which carries nixpkgs' harnesses rather than these pinned ones; its header
says what to run after a bump.

`journal/journal_test.go` checks retained journal compatibility, command deduplication,
transaction publication, coalesced receipts, rollback, and seals. `persistence_test.go` checks
session metadata and initialization log compatibility. `session_test.go`
checks native reply publication after translation commits and final frames at EOF.
`ingress/client_test.go` exercises real loopback gRPC replay, durable acknowledgements,
journal wakeups, backpressure and seal ordering; `ingress/supervisor_test.go` covers per-Session
worker discovery and joins. `transport_test.go` covers Runner wiring, COSI acknowledgement
observation and shutdown flushing. `process_test.go` checks parent-death fencing,
native descendants, inherited state ownership, and cancellation during blocked input.
These are not physical power-loss tests.
