# Environment end-to-end tests

This package exercises complete Sandbox environment topologies with real pinned Linux harnesses,
container process boundaries, and VM supervision. Production contracts live with their owners:
[Environment Runtime](../runtime/SPEC.md) and
[Execution Service](../runtime/execution/SPEC.md); protocol references are
[Runtime control](../runtime/docs/control.md), [Session protocol](../runtime/docs/session_protocol.md)
and [Codex executor integration](../runtime/execution/docs/codex_executor.md). Test helpers live in
[`testing/`](testing/), and this package defaults to test-only in Bazel.

## Tests

Process-supervision acceptance coverage (PID 1 and Execution companion containers, the VM systemd
fixture and VM scenarios) is described in
[supervision](../runtime/supervision/docs/supervision.md#acceptance-coverage).

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
`testing/driver.py`: it plays the Environments proxy's side of the control stream and reads each
session's Events from the stand-in ingress in `testing/gateway.py`;
the parametrized `model` fixture is the only place that knows the model API dialect. The scripted
model is shared with other packages' tests in [`agentplane/testing/`](../../testing):
`scripted_model.py` is the neutral vocabulary (`Text`, `Reasoning`, `ShellCall`, and the request
markers), and `claude_model.py` and `codex_model.py` speak the two dialects. Harness fixtures live
in `testing/`, where `launches.py` wires the pinned binaries to a scripted upstream. `test_restart.py`
runs the runner as its own process so a crash takes its harnesses with it. No test here runs the
image ([`image.nix`](../images/image.nix)), which carries nixpkgs' harnesses rather than these pinned ones; its header
says what to run after a bump.
The App, Sessions and native-contract integration tests reuse these fixtures through
[`runtime_fixtures.py`](../../app/testing/runtime_fixtures.py) (App), the `pytest_plugins` of
[`test_gateway_continuity.py`](../../session_dispatcher/test_gateway_continuity.py) (Sessions) and
[`native_service_fixtures.py`](../../testing/native_service_fixtures.py) (native contracts).

Durability unit tests live beside the code in [`env/runtime/`](../runtime) (`journal/`, `ingress/`,
`supervision/` and the Session process tests); none are physical power-loss tests.
