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
  Peer{{"Harness Host peer"}}
  BH{{"Bootstrap host / worker"}}
  PH{{"Runtime process host / workers"}}
  RC(["environment-runtime-close<br/>Close + transition sequence"])
  HI(["environment-host-intent"])
  HR(["environment-host-report"])
  BC(["bootstrap-lifecycle"])
  PC(["process-lifecycle"])
  EC(["embedded-execution-lifecycle"])
  EH{{"Embedded Execution host / service"}}
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
  RC -.->|coordinate effects| API
  API -.->|request start / fence| EH
  EH -->|publish| EI
  EI & ER -->|QPrimary / QMapped| EC
  EC -->|write| ER
  EC -.->|start / stop| EH
  EH -.->|VM exec-server launch| PH
  I -->|watch| HI
  HI -->|write applying / result| IS
  HI -.->|ApplyIntent| Peer
  I & IS -->|watch| HR
  Peer -.->|Report / ChangesSince| HR
  HR -->|write| R
  R -.->|read for matching intent| API
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

The optional embedded Execution controller shares the namespace and engine. Its host publishes `EmbeddedExecutionInputs`; `embedded-execution-lifecycle` reads those inputs and
its own `EmbeddedExecutionRuntimes`, and writes the latter. Its effect starts/stops the embedded
service; for a VM, that service uses the Runtime process host above to launch the native exec-server.
In a Pod the native executor is in the separate companion container. The Runtime's transition and
Close workers call and wait on these components, so those effect dependencies are not additional
COSI input subscriptions.

| Controller                     | Watched inputs                                                                                           | Controller output                                                       | Other relevant work                                                                                                      |
| ------------------------------ | -------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `environment-runtime-close`    | `EnvironmentRuntimeDesiredStates`, `EnvironmentRuntimeTransitionRequests` (weak)                         | `EnvironmentRuntimeCloses`, `EnvironmentRuntimeTransitions` (exclusive) | Close and adopt/quiesce are coordinated by this one controller; workers publish completion by waking its loop.           |
| `environment-host-intent`      | `HostIntents` (weak)                                                                                     | `HostIntentStatuses` (exclusive)                                        | Records applying before calling Host; an uncertain apply is not replayed.                                                |
| `environment-host-report`      | `HostIntents`, `HostIntentStatuses` (weak)                                                               | `HostReports` (exclusive)                                               | Also wakes on Host notifications and samples a report for the applied intent.                                            |
| `bootstrap-lifecycle`          | `BootstrapInputs` (primary); `BootstrapRuntimes`, `BootstrapOperationResults` (mapped)                   | `BootstrapRuntimes` (exclusive)                                         | Host owns admitted one-off work and publishes operation completion. Runtime admission checks the environment generation. |
| `process-lifecycle`            | `ProcessDesiredStates` (primary), `ProcessInputs` (mapped)                                               | `ProcessStatuses` (exclusive)                                           | Runtime host owns launch callbacks, live handles, observations and workers.                                              |
| `embedded-execution-lifecycle` | `EmbeddedExecutionInputs` (primary), `EmbeddedExecutionRuntimes` (mapped), both in `environment-runtime` | `EmbeddedExecutionRuntimes` (exclusive)                                 | Runtime host queues worker outcomes through its input; the controller retains an unfenced owner on failure.              |

## Harness Host

```mermaid
flowchart LR
  Peer{{"Runtime Host peer /<br/>Runner admission"}}
  SM{{"Session manager /<br/>one-off workers"}}
  AM{{"Codex app-server host /<br/>one-off workers"}}
  Journal{{"Journal / History ACK publishers"}}
  PH{{"Host process host / workers"}}
  RL(["runner-lifecycle"])
  SC(["session-lifecycle"])
  AC(["codex-app-server-lifecycle"])
  PC(["process-lifecycle"])
  RD["RunnerDesiredStates"]
  RI["RunnerInputs"]
  RR["RunnerRuntimes"]
  SI["SessionInputs"]
  SD["SessionDrainInputs"]
  SO["NativeOperationResults"]
  SR["SessionRuntimes"]
  AI["CodexAppServerInputs"]
  AO["CodexAppServerOperationResults"]
  AR["CodexAppServerRuntimes"]
  J["JournalSeals"]
  ACK["IngressAcks"]
  PD["ProcessDesiredStates"]
  PI["ProcessInputs"]
  PS["ProcessStatuses"]
  Native[("Session / setup / app-server<br/>process domains")]

  Peer -->|publish| RD & RI
  SM -->|publish| SI & SD & SO
  AM -->|publish| AI & AO
  Journal -->|publish| J & ACK
  PH -->|publish| PD & PI
  RD & RI & SD & SR & AR & J & ACK & PD & PS -->|watch| RL
  RL -->|write / queue next stage| RR
  SI & RD & RR & AR & SO -->|QPrimary / QMapped| SC
  SC -->|write| SR
  SC -.->|start / halt / seal| SM
  AI & RD & RR & AO -->|QPrimary / QMapped| AC
  AC -->|write| AR
  AC -.->|start / drain| AM
  SM & AM -.->|launch| PH
  PD & PI -->|QPrimary / QMapped| PC
  PC -->|write| PS
  PC -.->|start / stop| PH
  PH -.->|supervise and fence| Native
  RR -.->|sample for Host report| Peer
```

| Controller                   | Watched inputs                                                                                                                                                                            | Controller output                    | Selection / side effects                                                                                      |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| `runner-lifecycle`           | `RunnerDesiredStates`, `RunnerInputs`, `SessionDrainInputs`, `SessionRuntimes`, `JournalSeals`, `IngressAcks`, `CodexAppServerRuntimes`, `ProcessDesiredStates`, `ProcessStatuses` (weak) | `RunnerRuntimes` (exclusive)         | Aggregates the Host drain barrier; explicitly queues its next step after changing status.                     |
| `session-lifecycle`          | `SessionInputs` (primary); `RunnerDesiredStates`, `RunnerRuntimes`, `CodexAppServerRuntimes`, `NativeOperationResults` (mapped)                                                           | `SessionRuntimes` (exclusive)        | Per Session. App-server updates map only to Codex Sessions; operation results map to their Session.           |
| `codex-app-server-lifecycle` | `CodexAppServerInputs` (primary); `RunnerDesiredStates`, `RunnerRuntimes`, `CodexAppServerOperationResults` (mapped)                                                                      | `CodexAppServerRuntimes` (exclusive) | Per shared app-server; native effects run in host-owned one-off workers.                                      |
| `process-lifecycle`          | `ProcessDesiredStates` (primary), `ProcessInputs` (mapped)                                                                                                                                | `ProcessStatuses` (exclusive)        | The same single controller and registry shown above; per-domain contexts and worker barriers remain separate. |

The process controller and its three resource types appear in both diagrams to show their callers;
they are one controller and one set of resources. Each desired attempt records its process domain.
Runner aggregation considers only the Host domain, while Runtime-domain attempts can continue until
the Runtime's own barriers settle. Each registered callback, live handle and worker remains bound to
its owning domain's context and join barrier.

The Host publishes `SessionDrainInputs` separately from the more frequently updated Session input;
Runner aggregation needs inspection settlement, not every dispatch change. Journal seals and History
ACKs are facts published from durable journal/ingress activity. The in-memory resource is not the
durable journal, seal, or ACK authority.

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

### Runner drain and Runtime Close

```mermaid
flowchart LR
  Running --> Draining --> Halting --> Stopping
  Stopping -->|seal requested| Fencing -->|matching Execution fence| Sealing
  Stopping -->|no seal; Close| Closed
  Sealing -->|quiesce| Flushing -->|matching sealed ACKs| Quiesced
  Sealing -->|Close| Closed
```

Runner stages are aggregate status, with a blocker describing the first unmet predicate. Fencing
is needed only when a seal is requested and the matching fence is not yet present; an already
fenced generation can proceed directly to sealing. Closing with a seal waits for sealing, while
quiescing additionally waits for exact History ACK cursors. A new running generation can leave a
terminal drain state.

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
