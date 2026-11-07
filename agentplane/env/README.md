# Agentplane environment services

Environment Runtime owns Sandbox identity, bootstrap, external control, Session management,
harnesses, journals, History delivery and coordinated shutdown. Execution owns typed process and
filesystem operations and the native exec-server. The Runner Gateway remains outside `env/`.

`//agentplane/env/runtime/cmd:agentplane-environment-runtime` constructs those components in one
process and one COSI engine. Session records and acknowledgements have Runtime-owned persistent
COSI backing; Session journals retain SQLite storage under `<state_dir>/sessions`, and the shared
Codex home stays under `<state_dir>/codex`. `CreateSession` acknowledges a durable reservation;
harness startup follows admitted execution work.

`//agentplane/env/runtime/execution/cmd:agentplane-execution-service` provides the Pod execution
companion. Production Pod Runtime is PID 1 and owns the shared native reaper. VM native harnesses
and executors use separate systemd containment domains bound to Runtime's service invocation.

The sole environment desired lifecycle is `EnvironmentRuntimeDesiredStates`. Runtime closes its
admission gate before changing the desired revision. Independent Session, bootstrap, Execution and
process controllers publish their own observations. Drain preserves native process fencing, journal
sealing and exact durable History acknowledgements. Runtime joins delivery, controllers, workers and
journals before releasing its environment owner. See the [Runtime contract](runtime/SPEC.md),
[lifecycle ordering](runtime/docs/lifecycle.md) and [COSI map](runtime/docs/cosi.md).

Native adapters, Session management, retained records, journals and workspace helpers live directly
under `env/runtime/`. Shared process machinery is in `runtime/supervision`, one-shot operation
bookkeeping in `env/oneoff`, shell execution in `env/scripts`, and Go deployment parsing in
`env/configuration`. `env/e2e/` contains cross-service integration tests; image and VM packaging
lives in `env/images/`.

The deployment renders the shared [Session guide](../session_instructions/platform_instructions.j2)
with concrete service URLs. Native and central backends append the child's Session ID and standing
instructions. Detailed Action workflows are available through `agentplane actions guide`;
Notifications serves its own `/llms.txt` index.

The Runtime and companion ship with the `agentplane` CLI in `//agentplane/env/images:disk_image`.
Native Claude/Codex executables, workspace tools and CA certificates come from the Runtime image.
