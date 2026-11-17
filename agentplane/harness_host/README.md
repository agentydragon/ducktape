# HarnessHost

The Session half of a harness host: `CreateSession`, the four commands, `ListSessions` and
`WatchSessions` ([`harness_host.proto`](harness_host.proto)). Two hosts serve it: the Environment
Runtime for Sessions whose native harness runs in its Sandbox, and the central sampler for
`central_sampler_v1` Sessions. Sandbox lifecycle stays on `EnvironmentRuntime`.

The Runtime serves HarnessHost beside `EnvironmentRuntime`
(`env/runtime/internal/coordinator/harness_host.go`), and its commands pass the same lifecycle
admission fence, so `Quiesce` and `PrepareSuspend` close them. Environments forwards it per Sandbox
with the [Runtime proxy](../environments/runtimeproxy/SPEC.md)'s routing, incarnation fencing and
status version check.

## Session status

`ListSessions` and `WatchSessions` return one `Session` per Session the host holds. The common part is
the Session's own loop on any host: its active turn and the commands admitted but not yet applied.
Everything about the host or a Sandbox sits in the `host` oneof, whose kind always equals the Session's
placement kind. Operator decision, 2026-11-17: setup stays per Session until a central Session can have
detached or multiple attachments (`SESSION_ENVIRONMENT_SETUP_DESIGN`).

- **Setup** runs once per Session, after Sandbox bootstrap, in the Session's cwd
  (`env/runtime/session/setup.go`); the harness starts only after it succeeds. Two Sessions on one
  Sandbox each run their own script. The sampler runs it through the Executor on the executor Sandbox,
  and a failure or interruption blocks that Session for good because setup is never replayed
  (`central_sampler/internal/sessionstore/execution.go`). `Blocked` has no setup cause: `setup`
  already says FAILED or INTERRUPTED.
- **Harness attachment** is this Session's hold on a native process, not the Sandbox's state: a Ready
  Sandbox holds detached and attached Sessions side by side. A command's demand starts the harness
  (`env/runtime/session/controller.go`) and a process exit detaches the Session. A launch failure is a
  fact about the last attempt, cleared by the next `HarnessStarted`, so it is `failure_cursor`, not a
  state.
- **Blocked causes** come from the sampler's `findBlockingCause` in order: an unresolved intent, a cell
  with no host, a process with no terminal result, then a generic unconfirmed outcome
  (`central_sampler/internal/sessionstore/commands.go`). Their readers are the operator's recovery
  runbook and tests.
- **Turn outcomes** are not status. Both hosts journal `TurnCompleted` with its status and error, and
  History keeps `last_turn_status`.
- **Sandbox busy** (autosuspend blockers, including activation and bootstrap) stays on
  `EnvironmentRuntime.WatchState` for the Sandbox Controller; HarnessHost reports no Sandbox-level
  fact. Process ID, native session ID and failure cursor are diagnostics on `GetStatus`.
