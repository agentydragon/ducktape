# MCP fixture shutdown, 2 November 2026

## Cause and repair

The retained MCP stack spent an extra 30 seconds deleting its main namespace.
Containerd logs from both instrumented MCP Delegation controls identify the
`mcp-everything` fixture: SIGTERM, a 30-second wait, SIGKILL, then exit 137.
The inspected other fixture containers stopped in less than a second.

The exact pinned `tzolov/mcp-everything-server` image
(`sha256:96c4aa07420dd2a8dee0315763a8ea27de72fd054483c781894f6280cd3f56e7`)
runs Node as PID 1. Its `/app/dist/streamableHttp.js` handles SIGINT by closing
active transports and exiting zero, but does not register a SIGTERM handler.
This conclusion comes from the pinned image's application layer, not current
upstream source.

Give this fixture a Kubernetes preStop hook that sends SIGINT to PID 1. It uses
the image's existing shell and the server's existing graceful shutdown path.
Keep its image, termination grace, readiness probes and security context.

## Experiment

[Instrumented control](https://app.buildbuddy.io/invocation/49ca8687-4734-43c8-824a-6601ed7bc9ed):
effective source `471205dc015fa0ca84cd93d8eec5871db34088d6` plus cleanup spans.
Two ordinary MCP Delegation runs passed, with cleanup taking 59.2/59.7 seconds.
Both `mcp-everything` containers exited 137 after the full 30-second wait.

[Namespace-overlap comparison](https://app.buildbuddy.io/invocation/a80384d4-b519-4a60-a387-09d65660a971):
the same source plus overlapping fixture namespace deletion. MCP Delegation
cleanup took 54.4/56.3 seconds. Namespace overlap is independently proposed in
[PR #13855](https://github.com/agentydragon/ducktape/pull/13855); it is not part of
this signal repair.

[Signal candidate](https://app.buildbuddy.io/invocation/a82b07db-0922-4dc8-9ecc-8c7677d62afc):
the same instrumented, overlapping-namespace source plus the preStop hook.
All four ordinary MCP suites passed twice: 24 scenario executions, unchanged
assertions, and eight completed cleanup receipts. The five existing render tests
also passed twice; the changed Python libraries built with lint aspects.

All eight candidate `mcp-everything` containers exited zero without SIGKILL.
Containerd StopContainer calls returned in 0.12–0.23 seconds; the exit event can
precede that call because preStop already signaled the process. These call
durations do not measure the preceding hook itself.

MCP Delegation cleanup took 21.9/30.6 seconds, a mean 29.1-second reduction
against the namespace-overlap comparison. The other six MCP cleanup samples
took 22.2–23.6 seconds. Daemon events establish that the grace-period wait
disappeared; resource-absence and baseline certification requirements remain.

The test command was:

```sh
bbr test //agentplane/system_tests/suites/mcp:test_actions \
  //agentplane/system_tests/suites/mcp:test_services \
  //agentplane/system_tests/suites/mcp:test_delegation \
  //agentplane/system_tests/suites/mcp:test_parent_delegation \
  //agentplane/system_tests/k3s:test_render \
  //agentplane/system_tests/k3s:mcp_fixture_servers \
  //agentplane/system_tests/k3s:stack \
  --nocache_test_results --runs_per_test=2
```

All selected baselines were cold. Worker/setup/scenario variation and the small
sample preclude a fleet CI feedback estimate. The standalone signal PR retains
serial namespace deletion until the independent overlap change lands. Raw
execution responses, traces, JUnit, receipts and daemon logs remain in task scratch.
