# HarnessHost Session status

**Undecided design.** This is the per-Session status that both HarnessHost hosts (the Environment Runtime and the
central sampler) would return from `ListSessions` and stream from `WatchSessions`, and what the Session
Dispatcher's running status would record from it. The operator asked for it on 2026-11-17 ("sketch out
the one status shape") and reviewed a first sketch at 06:22 UTC. Their points shaped this revision:

- setup might belong to an attached Sandbox, or the feature might go;
- the Session execution state seemed odd;
- `blocked_reason` should perhaps be an enum;
- a busy-reason enum "kinda smooths over "the _sandbox_ is busy" and "the _session_'s sampling loop is
  being driven"".

The operator has not accepted the shape as a whole or released building it. Setup stays per Session for now. Operator, 06:28 UTC: "the
conservative thing would be to keep setup per session for now if we can, but we should revisit it
before central sampler can have detached sessions / multiple attachment"
([`SESSION_ENVIRONMENT_SETUP_DESIGN`](task_dag.md#session_environment_setup_design--choose-where-setup-belongs)).

Consumers: [`HARNESS_HOST_EXTRACTION`](task_dag.md#harness_host_extraction--pull-harnesshost-out-of-the-environment-runtime),
[`CENTRAL_SAMPLER_HARNESS_HOST`](task_dag.md#central_sampler_harness_host--the-central-sampler-serves-harnesshost)
and [`DISPATCHER_RUNNING_STATUS`](task_dag.md#dispatcher_running_status--the-dispatcher-records-running-status).

## Shape

The common part is the Session's own loop on any host. Everything about the host or the Sandbox sits in
a `host` oneof whose kind always equals the Session's placement kind. On devel, HarnessHost
`ListSessions` and `WatchSessions` carry `agentplane.proto.SessionSummary`; nothing calls either
through HarnessHost yet, so this would replace it.

```proto
// agentplane.harness_host
message Session {
  string session_id = 1;
  // The Session's own loop, on any host.
  string active_turn_id = 2;     // empty when no turn is being driven
  uint32 pending_commands = 3;   // admitted by this host, not yet applied
  oneof host {                   // same kind as spec.placement
    EnvironmentHostedSession environment_hosted = 4;
    CentralSamplerSession central_sampler_v1 = 5;
  }
}

// The native harness runs in this Session's Sandbox.
message EnvironmentHostedSession {
  agentplane.proto.SetupState setup = 1;   // this Session's script, in its cwd on this Sandbox
  HarnessAttachment harness = 2;
}

// This Session's hold on a native harness process: its own, or a shared Codex app-server.
message HarnessAttachment {
  enum State {
    STATE_UNSPECIFIED = 0;
    STATE_DETACHED = 1;   // no live process holds it; the next command starts or resumes one
    STATE_STARTING = 2;   // a command's demand is starting or resuming the native session
    STATE_ATTACHED = 3;   // a live process holds it
  }
  State state = 1;
  optional uint64 failure_cursor = 2;   // launch failure or unexpected exit since the last start
}

// The sampler drives the loop; tools run on the executor Sandbox when there is one.
message CentralSamplerSession {
  agentplane.proto.SetupState setup = 1;   // the script on the executor; NOT_REQUIRED without one
  optional Blocked blocked = 2;            // set while the sampler refuses to continue
}

message Blocked {
  oneof cause {
    UnresolvedIntent unresolved_intent = 1;     // intent ID and kind
    OrphanedCell orphaned_cell = 2;             // a JavaScript cell with no live host
    UnsettledProcess unsettled_process = 3;     // an executor process with no terminal result
    UnconfirmedEffects unconfirmed_effects = 4; // execution ended unconfirmed; no handle named
  }
  string detail = 5;   // the driver error, for the operator
}

message ListSessionsResponse { repeated Session sessions = 1; }

// Snapshot upserts, CaughtUp, then changed upserts and CaughtUp. Every stream starts with a full
// snapshot; there is no resume token.
message WatchSessionsResponse {
  oneof update {
    Session upsert = 1;
    CaughtUp caught_up = 2;
  }
}
```

## Where each fact sits, from the code

- **Setup** runs once per Session, after Sandbox bootstrap, in the Session's cwd
  (`env/runtime/session/setup.go`); the harness starts only after it succeeds. Two Sessions on one
  Sandbox each run their own script in their own workspace. The sampler runs it through the Executor
  on the executor Sandbox, and a failure or interruption blocks that Session for good because setup is
  never replayed (`central_sampler/internal/sessionstore/execution.go`). Being work on a Sandbox, it
  sits on the host side; `Blocked` has no setup cause because `setup` already says FAILED or
  INTERRUPTED.
- **Harness attachment** replaces `SessionExecutionState` (`env/runtime/session/status.go`). That state
  tracks this Session's hold on a native process, not the Sandbox: a Ready Sandbox holds dormant and
  attached Sessions side by side. A command's demand starts preparation
  (`env/runtime/session/controller.go`); a process exit returns the Session to dormant. FAILED is a fact
  about the last attempt, cleared by the next `HarnessStarted`, so it becomes `failure_cursor`.
- **Blocked causes** come from the sampler's reason builder: an unresolved intent, then a cell with no
  host, then a process with no terminal result, then a generic unconfirmed outcome
  (`central_sampler/internal/sessionstore/commands.go`). Today they are prose. Their readers are the
  operator's recovery runbook and tests.
- **Turn outcomes** are not on the status. Both hosts journal `TurnCompleted` with its status and
  error, and History keeps `last_turn_status`. Where turn states should live is the later to-do
  [`TURN_STATE_PLACEMENT_DESIGN`](task_dag.md#turn_state_placement_design--decide-where-turn-states-live).
- **Sandbox busy** (autosuspend blockers, including activation and bootstrap) stays on
  `EnvironmentRuntime.WatchState` for the Sandbox Controller. HarnessHost reports no Sandbox-level fact.
- **Dropped from `SessionSummary`:**
  - `harness_state` means "a PID exists" on the Runtime and is invented on the sampler.
  - `launch_outcome` stays in History's `Attached` projection.
  - `spec` and `last_cursor` are held by the Dispatcher and History.
- **Stays in `GetStatus` diagnostics:** `process_id`, `native_session_id` and `failure_cursor`.

## Dispatcher running status

The Dispatcher's Session status (`SessionStatus`, field 6) records the host's last upsert verbatim:

```proto
message SessionActivity {
  google.protobuf.Timestamp recorded_at = 1;
  oneof kind {
    agentplane.harness_host.Session observed = 2;
    Unknown unknown = 3;
    EnvironmentDown environment_down = 4;
  }
  message Unknown {
    enum Reason { REASON_UNSPECIFIED = 0; NOT_YET_OBSERVED = 1; STREAM_LOST = 2; }
    Reason reason = 1;
  }
  message EnvironmentDown {
    enum Cause { CAUSE_UNSPECIFIED = 0; SUSPENDED = 1; DELETED = 2; }
    Cause cause = 1;
  }
}
```

- **Hosted Session:**
  - Observed once its Sandbox's stream has caught up.
  - Unknown(STREAM_LOST) from the moment that stream ends until the next CaughtUp.
  - EnvironmentDown when the Sandbox is suspended or deleted.
  - The stream is the incarnation fence: the Environments proxy ends a forwarded stream when its
    Runtime connection ends, so no incarnation field rides on the frames.
- **Central Session:**
  - Observed from the sampler's one stream, whatever its executor's state. That includes a Session
    with no Environment, whose host is the sampler.
  - Unknown(STREAM_LOST) while that stream is down.
- **Archive check:** archiving would hide work still producing output when there is an active turn,
  pending commands or setup running, so it refuses then. It also refuses on Unknown.
  - Activation needs no field: the Runtime starts a harness only on command demand, which
    `pending_commands` already counts.
