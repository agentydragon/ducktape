# Environment Runtime COSI map

This is a map of the controllers and resources registered in the Runtime's **single in-memory
COSI engine** and the single `environment-runtime` COSI namespace. That namespace is not a
process, cgroup, Kubernetes namespace, or durable store. Harness Host registers its controllers
into the same engine. The embedded Execution controller exists only when Execution
is configured; the Codex app-server controller exists only when Codex is configured.

Resource names below omit the common `.agentplane` suffix. A solid arrow means a resource is
published/written or read/watched; a dashed arrow is an in-process call, worker effect, or
notification. Boxes are resources, rounded boxes are controllers, and the other shapes are host
code or external effects. A controller may read its own output without subscribing to it. The
`QPrimary` and `QMapped` distinctions in the tables describe **event subscriptions**, not all
synchronous reads. Host publishers and one-off workers are not COSI controllers.

## Runtime coordination

```mermaid
flowchart LR
  API{{"Runtime API / lifecycle owner"}}
  BH{{"Bootstrap host / worker"}}
  PH{{"Runtime process host / workers"}}
  RC(["environment-runtime-close<br/>Close + transition sequence"])
  HO(["environment-host-report-observer"])
  BC(["bootstrap-lifecycle"])
  PC(["process-lifecycle"])
  EC(["embedded-execution-lifecycle"])
  EH{{"Embedded Execution host / service"}}
  CW{{"Close / transition workers"}}
  ED["EnvironmentRuntimeDesiredStates"]
  TR["EnvironmentRuntimeTransitionRequests"]
  TS["EnvironmentRuntimeTransitions"]
  CL["EnvironmentRuntimeCloses"]
  I["HostIntents"]
  IS["HostIntentStatuses"]
  R["HostReports"]
  BI["BootstrapInputs"]
  BO["BootstrapOperationResults"]
  BR["BootstrapRuntimes"]
  PD["ProcessDesiredStates"]
  PI["ProcessInputs"]
  PS["ProcessStatuses"]
  EI["EmbeddedExecutionInputs"]
  ER["EmbeddedExecutionRuntimes"]
  Native[("Bootstrap scripts / VM exec-server<br/>and process domain")]

  API -->|publish| ED & TR & I
  ED & TR -->|watch| RC
  RC -->|write| CL & TS
  RC -.->|start one-shot barriers| CW
  CW -.->|completion channel| RC
  CW -.->|bound waits / effects| API
  API -.->|request start / fence| EH
  EH -->|publish intent + completion facts| EI
  EI & ER -->|QPrimary / QMapped| EC
  EC -->|write| ER
  EC -.->|start / stop| EH
  EH -.->|VM exec-server launch| PH
  IS & R -->|watch| HO
  HO -.->|wake / fence| API
  R -.->|read intent and report| API
  IS -.->|read exact applied status| API
  API -.->|request| BH
  BH -->|publish| BI & BO
  BI & BO & BR -->|QPrimary / QMapped| BC
  BC -->|write| BR
  BC -.->|start operation| BH
  BH -.->|launch script| PH
  PH -->|publish| PD & PI
  PD & PI -->|QPrimary / QMapped| PC
  PC -->|write| PS
  PC -.->|start / stop| PH
  PH -.->|supervise and fence| Native
  PS -.->|read / watch| PH
```

The optional embedded Execution controller shares the namespace and engine. Its host publishes `EmbeddedExecutionInputs` with desired UID/demand and generation-bound start, loss and fence completion facts; `embedded-execution-lifecycle` reads those inputs and
its own `EmbeddedExecutionRuntimes`, and writes the latter. Its effect starts/stops the embedded
service; for a VM, that service uses the Runtime process host above to launch the native exec-server.
In a Pod the native executor is in the separate companion container. The Runtime's transition and
Close workers call and wait on these components, so those effect dependencies are not additional
COSI input subscriptions.

The coordinator keeps its active plan and worker handles in process. Each barrier reports through
its completion channel; the published transition or Close status is the observable progress, not
a durable work queue. A blocking Host binding check stays in a worker so Close can preempt a
transition. An uncertain effect retains ownership and is not automatically replayed.

| Controller                         | Watched inputs                                                                                           | Controller output                                                       | Other relevant work                                                                                                           |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `environment-runtime-close`        | `EnvironmentRuntimeDesiredStates`, `EnvironmentRuntimeTransitionRequests` (weak)                         | `EnvironmentRuntimeCloses`, `EnvironmentRuntimeTransitions` (exclusive) | Close and adopt/quiesce are coordinated by this one controller; workers publish completion by waking its loop.                |
| `environment-host-report-observer` | `HostReports`, `HostIntentStatuses` (weak)                                                               | None                                                                    | Wakes Runtime waiters and starts the Execution fence for an exact applied report.                                             |
| `bootstrap-lifecycle`              | `BootstrapInputs` (primary); `BootstrapRuntimes`, `BootstrapOperationResults` (mapped)                   | `BootstrapRuntimes` (exclusive)                                         | Runtime owns admitted bootstrap work and publishes operation completion. Runtime admission checks the environment generation. |
| `process-lifecycle`                | `ProcessDesiredStates` (primary), `ProcessInputs` (mapped)                                               | `ProcessStatuses` (exclusive)                                           | Runtime host owns launch callbacks, live handles, observations and workers.                                                   |
| `embedded-execution-lifecycle`     | `EmbeddedExecutionInputs` (primary), `EmbeddedExecutionRuntimes` (mapped), both in `environment-runtime` | `EmbeddedExecutionRuntimes` (exclusive)                                 | Runtime host publishes typed completion facts; the controller retains an unfenced owner on failure.                           |

## Harness Host

```mermaid
flowchart LR
  Peer{{"Environment Runtime peer /<br/>Harness Host admission"}}
  SM{{"Session manager /<br/>one-off workers"}}
  AM{{"Codex app-server host /<br/>one-off workers"}}
  Journal{{"Journal / History ACK publishers"}}
  PH{{"Host process host / workers"}}
  RL(["Harness Host lifecycle<br/>(runner-lifecycle)"])
  SC(["session-lifecycle"])
  AC(["codex-app-server-lifecycle"])
  PC(["process-lifecycle"])
  RD["RunnerDesiredStates"]
  RI["RunnerInputs"]
  RR["RunnerRuntimes"]
  R["HostReports"]
  HR(["harness-host-report"])
  Guard{{"Synchronous connection /<br/>admission guards"}}
  I["HostIntents"]
  IS["HostIntentStatuses"]
  HI(["harness-host-intent"])
  SI["SessionInputs"]
  SD["SessionDrainInputs"]
  SO["NativeOperationResults"]
  SR["SessionLifecycleStatuses"]
  AI["CodexAppServerInputs"]
  AO["CodexAppServerOperationResults"]
  AR["CodexAppServerRuntimes"]
  ER["EmbeddedExecutionRuntimes"]
  J["JournalSeals"]
  ACK["IngressAcks"]
  PD["ProcessDesiredStates"]
  PI["ProcessInputs"]
  PS["ProcessStatuses"]
  Native[("Session / setup / app-server<br/>process domains")]

  Peer -->|publish| RD & RI
  I -->|watch| HI
  HI -->|write applying / result| IS
  Guard -.->|exact boot IDs and startup| HI
  HI -.->|admitted native apply| Peer
  Guard -.->|exact binding on direct calls and effects| Peer
  SM -->|publish| SI & SD & SO
  AM -->|publish| AI & AO
  Journal -->|publish| J & ACK
  PH -->|publish| PD & PI
  RD & RI & IS & SD & SR & AR & J & ACK & PD & PS -->|watch| RL
  RL -->|write / queue next stage| RR
  SI & RD & RR & AR & SO -->|QPrimary / QMapped| SC
  SC -->|write| SR
  SC -.->|start / halt / seal| SM
  AI & RD & RR & AO & ER -->|QPrimary / QMapped| AC
  AC -->|write| AR
  AC -.->|start / drain| AM
  SM & AM -.->|launch| PH
  PD & PI -->|QPrimary / QMapped| PC
  PC -->|write| PS
  PC -.->|start / stop| PH
  PH -.->|supervise and fence| Native
  RR & RD -->|watch| HR
  Peer -.->|inventory / idle / admission facts| HR
  HR -->|write sampled intent + report| R
```

| Controller                                  | Watched inputs                                                                                                                                                                                                           | Controller output                      | Selection / side effects                                                                                                                      |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `harness-host-report`                       | `RunnerDesiredStates`, `RunnerRuntimes` (weak), plus Host change wakeups                                                                                                                                                 | `HostReports` (exclusive)              | Publishes inventory, blocker, idle and lifecycle facts with the full sampled intent.                                                          |
| `harness-host-intent`                       | `HostIntents` (weak)                                                                                                                                                                                                     | `HostIntentStatuses` (exclusive)       | Checks startup and the synchronous Runtime connection gate, records Applying before the native effect, and fails closed on an unknown result. |
| Harness Host lifecycle (`runner-lifecycle`) | `RunnerDesiredStates`, `RunnerInputs`, `HostIntentStatuses`, `SessionDrainInputs`, `SessionLifecycleStatuses`, `JournalSeals`, `IngressAcks`, `CodexAppServerRuntimes`, `ProcessDesiredStates`, `ProcessStatuses` (weak) | `RunnerRuntimes` (exclusive)           | Aggregates the Host drain barrier; explicitly queues its next step after changing status.                                                     |
| `session-lifecycle`                         | `SessionInputs` (primary); `RunnerDesiredStates`, `RunnerRuntimes`, `CodexAppServerRuntimes`, `NativeOperationResults` (mapped)                                                                                          | `SessionLifecycleStatuses` (exclusive) | Per Session. App-server updates map only to Codex Sessions; operation results map to their Session.                                           |
| `codex-app-server-lifecycle`                | `CodexAppServerInputs` (primary); `RunnerDesiredStates`, `RunnerRuntimes`, `CodexAppServerOperationResults`, `EmbeddedExecutionRuntimes` (mapped)                                                                        | `CodexAppServerRuntimes` (exclusive)   | Per shared app-server; native effects run in host-owned one-off workers. Execution changes wake a separate live-owner readiness check.        |
| `process-lifecycle`                         | `ProcessDesiredStates` (primary), `ProcessInputs` (mapped)                                                                                                                                                               | `ProcessStatuses` (exclusive)          | The same single controller and registry shown above; per-domain contexts and worker barriers remain separate.                                 |

The process controller and its three resource types appear in both diagrams to show their callers;
they are one controller and one set of resources. Each desired attempt records its process domain.
Harness Host aggregation considers only its domain, while Environment Runtime attempts can continue until
its own barriers settle. Each registered callback, live handle and worker remains bound to
its owning domain's context and join barrier.

Harness Host publishes `SessionDrainInputs` separately from the more frequently updated Session input;
Harness Host aggregation needs inspection settlement, not every dispatch change. Journal seals and History
ACKs are facts published from durable journal/ingress activity. The in-memory resource is not the
durable journal, seal, or ACK authority.

The retained Session record supplies immutable configuration. `SessionInputs` carries harness kind
and demand alongside the setup, process, dispatch and inspection facts the controller reconciles.
`SessionLifecycleStatuses` records controller progress such as phase, attempt and native generation;
it is not desired runtime configuration. Reconciliation starts a dedicated Claude process per Session
or attaches a Codex Session thread to the shared app-server. A Codex Session's process ID identifies
that shared app-server; its per-Session attachment and router state remains in memory.

Runtime synchronously revokes the native connection gate when its Host connection ends or Runtime
fails, including when a COSI write fails. Host admission also checks the exact binding under its
mutex before durable work. The Host intent controller records Applying before calling the native
apply path; an interrupted application fails closed rather than replaying the effect. The report
controller wakes on both Runner COSI changes and Host inventory notifications, and snapshots the
full intent. Runtime accepts a report only after that exact intent is applied, so a same-generation
Execution fence cannot validate an older report.

`EnvironmentRuntimeDesiredStates`, `HostIntents`, statuses, reports and operation results are
in-memory coordination state. `BootstrapSelections` alone has a COSI backing store; `BootstrapInputs`
is live-only. The incarnation seal, bootstrap event log, Session records,
journals and History acknowledgements have durable owners. Owner locks have process lifetimes even
though their lock files remain on disk; see [lifecycle ownership](lifecycle.md#durable-owners). A
process exit observation alone does not prove that its native process domain was fenced.

## State and barriers

These diagrams show the main paths and meaningful alternatives; reconciliation can wait at any
state for its predicates or report an error. A new desired generation can reset component state;
these are not global sequences shared by every Session or process.

### Process attempt

```mermaid
stateDiagram-v2
  [*] --> Pending
  Pending --> Starting: finalizer installed
  Starting --> Running: start observed
  Starting --> Stopping: exit or teardown with cleanup pending
  Starting --> Exited: failed start and fenced
  Running --> Stopping: exit or teardown
  Pending --> Stopping: teardown
  Stopping --> Exited: process domain fenced
  Exited --> [*]: desired state destroyed
```

The controller retains its finalizer through uncertain starts and failed fences. `Exited` and
finalizer removal require the host's fence proof for a registered attempt. A systemd start reply,
unit snapshot, or leader exit in `ProcessStatuses` is an observation, not that proof. A retired
attempt ID is not reused. See [supervision](../supervision/docs/supervision.md).

### Embedded Execution and bootstrap

```mermaid
stateDiagram-v2
  state Execution {
    [*] --> Stopped
    Stopped --> Starting: admitted demand
    Starting --> Ready: current start succeeds
    Starting --> Stopped: superseded and fenced
    Starting --> Stopping: start succeeds after demand changes
    Starting --> Failed: start error
    Ready --> Stopping: loss or changed demand
    Stopping --> Stopped: fence succeeds
    Stopping --> Failed: fence or retained error
  }
  state Bootstrap {
    [*] --> Dormant
    Dormant --> Running: admitted request
    Running --> Succeeded: completed
    Running --> Failed: error
    Running --> Interrupted: interrupted
  }
```

Execution can recover a lost service only after fencing and subject to the same binding/demand
checks. Bootstrap is a retained one-shot attempt; its request file and event log are durable.

### Session and shared Codex app-server

```mermaid
stateDiagram-v2
  state Session {
    [*] --> Dormant
    Dormant --> Preparing: admitted demand
    Preparing --> Ready: setup and launch complete
    Preparing --> Dormant: failed or superseded
    Ready --> Dormant: process lost
    Dormant --> Stopping: Host drain
    Preparing --> Stopping: Host drain
    Ready --> Stopping: Host drain
    Stopping --> Halted: halt completes
  }
  state CodexAppServer {
    [*] --> Stopped
    Stopped --> Initializing: admitted demand
    Initializing --> Ready: initialization succeeds
    Initializing --> Stopped: failed or superseded
    Initializing --> Draining: stop needed
    Ready --> Draining: Host drain
    Ready --> Stopped: process exit observed
    Draining --> Stopped: cleanup settles
  }
```

Session halt and process fencing are separate facts. The aggregate also waits for pending
inspections and the app-server. A Session can remain halted while its process or journal barrier is
still incomplete.

### Harness Host drain and Runtime Close

```mermaid
flowchart LR
  Running --> Draining --> Halting --> Stopping
  Stopping -->|seal requested| Fencing -->|matching Execution fence| Sealing
  Stopping -->|no seal; Close| Closed
  Sealing -->|quiesce| Flushing -->|matching sealed ACKs| Quiesced
  Sealing -->|Close| Closed
```

Host stages are aggregate status, with a blocker describing the first unmet predicate. Admission
must settle before Sessions halt; Host then waits for Session processes, the shared app-server,
Host-owned process finalizers and pending inspections. After that native stop, a seal-bearing intent
must have an exact, successfully applied Host intent with the Execution fence before sealing begins.
Closing with a seal waits for sealing, while quiescing additionally waits for exact History ACK
cursors. A new running generation can leave a terminal drain state.

```mermaid
flowchart LR
  Preparing --> NativeStopping --> ExecutionFencing --> HostFlushing --> HostJoining --> BootstrapSettling --> ReadyToJoin
  Preparing & NativeStopping & ExecutionFencing & HostFlushing & HostJoining & BootstrapSettling -->|error| Failed
```

`ReadyToJoin` permits the outer Runtime to stop the engine and join its workers; it is not itself
owner release. Adopt and quiesce transitions are separately sequence-bound requests handled by the
same controller. Their planned stages depend on current durable state and bindings; see
[transition planning](../transition_adoption.go) and [quiesce planning](../transition_quiesce.go).

## Source of truth and ownership boundary

Controller subscriptions and outputs are declared in each controller's `Settings` or `Inputs` and
`Outputs`. Start with [Runtime registration](../runtime.go), [Host registration](../harnesshost/lifecycle_runtime.go),
[resource contracts](../lifecycle/contracts.go), and the controller files linked from those
registrations.

One process registry supplies the single process controller. Runtime and Harness Host obtain
separate scoped views: they share attempt identities and reconciliation, but each attempt retains
its owner's context, error callback and worker wait group. The explicit `ProcessDesiredStates`
domain identifies which attempts belong to the Host drain barrier. Neither the shared COSI
namespace nor the shared controller merges native process domains, VM systemd slices, owner locks,
or the cleanup proof for any individual attempt.
