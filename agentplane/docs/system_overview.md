# Agentplane system overview

## System overview

Service boundaries and traffic, independent of environment or replica layout. API arrows point
caller to callee; labels such as `response` show data on that edge; dashed arrows are
runner-initiated platform routes. Per-path diagrams (session delivery, runner control,
history and browser sync, notifications, actions and egress) and direct API definition links:
[focused diagrams below](#focused-diagrams).

The Controller serves EnvironmentService on each replica at `sandbox-controller:8082`, with
`spiffe://agentplane/sandbox-controller`. Its independent policy watches and uncached Kubernetes
reads do not require leadership. App and Actions use this receiver with service SVIDs; Actions
preserves typed delegation. EnvironmentService rejects Kubernetes bearer credentials and performs no
TokenReview; its exact reader, manager, launcher and operator SVID roles remain independent. The Gateway has no direct Runtime access, discovery RBAC or proxy grants. Runtime proxy, HarnessHost and Executor calls target this same Environments listener. The Gateway serves Dispatcher and health only; it registers no Runtime proxy, HarnessHost, Executor or
EnvironmentService and opens no direct Runtime watch.

EnvironmentService also serves an App-only `GetRuntimeDiagnostics` read through its replica-local,
incarnation/boot-fenced Runtime transport. It is independent of controller leadership and never
wakes an environment. The App uses this RPC for on-demand diagnostics. Session running marks use Dispatcher's
`WatchSessions` through the App's plain HTTP proxy; the App-owned archive mutation checks
Dispatcher `GetSession` and refuses unknown activity or unfinished work.

Managed Sandbox CLIs use a separate `AGENTPLANE_ENVIRONMENTS_URL` on the local platform listener.
Its exact EnvironmentService prefix selects Sandbox-SVID TLS through central CONNECT to
`environments:8082`. Managed Session calls use `sessions:8080`.

The root-owned `sessions:8080`, `runner-gateway:8080` and `session-dispatcher:8080` Services
select the `sessions` and retained `runner-gateway` Deployments through a shared routing label.
Both Deployments use the same queue database, durable delivery leases and server SVID
`spiffe://agentplane/session-dispatcher`. Their Pod and Deployment selectors remain distinct.
The `environments:8082` and `sandbox-controller:8082` Services select both the `environments`
and retained `sandbox-controller` Deployments through a shared routing label, with distinct
Deployment selectors. Both retain `spiffe://agentplane/sandbox-controller` and share the
`agentplane-sandbox-controller` leader-election Lease. Central CONNECT admits both new authorities beside
the old ones. The App targets `sessions:8080` and `environments:8082`; Notifications uses
`sessions:8080` for Session validation and notice submission. Actions uses both new Services for
orchestration and Session delegation. Those callers and the managed Sandbox relays keep the existing
server SVID pins. The controller's single metrics scrape selects the `environments` Service.
OpenTelemetry service names remain `runner-gateway` for Sessions and `sandbox-controller` for Environments.

```mermaid
flowchart LR
    browser([Browser])
    liveTests([Manual live system tests])
    httpsGateway[HTTPS Gateway]
    frontend[Static frontend]
    app[Integration API]
    subgraph services[Agentplane services]
        direction TB
        gw["Sessions TLS RPC :8080: Dispatcher<br/>sessions and retained runner-gateway Deployments"]
        sandboxctl["Environments :8082: EnvironmentService, Runtime proxy, HarnessHost and Executor<br/>environments and retained sandbox-controller Deployments"]
        sessionsService[sessions Service :8080]
        environmentsService[environments Service :8082]
        sessionsService -->|shared routing label; unchanged server SVID| gw
        environmentsService -->|shared routing label and unchanged server SVID| sandboxctl
        policy[Policy Controller]
        hs[History SVID-only TLS RPC :8080: readers and JournalIngress]
        notif[Notification TLS RPC :8080; signed webhook HTTP :8081]
        actions[Action TLS RPC :8080]
        edge["MCP edge<br/>mTLS gRPC 8443 + HTTP 8081"]
        egress[Egress proxy]
        egressRules[Rules and EgressAdmin TLS gRPC :8081]
        llm[LLM inference HTTP and ModelConfig RPC over TLS :8080]
        electric[Electric]
        sampler[Central sampler TLS gRPC :8083]
        samplerdb[(Sampler PostgreSQL)]
    end
    subgraph runtimes[Sandbox environments]
      direction LR
      subgraph sandbox[Sandbox Pod]
        subgraph pod_runtime[Environment Runtime process: PID1]
          runtime[Environment lifecycle]
          runner[Runtime Session components + native drivers]
          execution[Execution API]
        end
        runtime -->|direct lifecycle and Session calls| runner
        runtime -->|lifecycle and execution RPC| execution
        execution -->|companion ownership: gRPC over Unix socket| podexec
        agent[Managed agent process and CLI]
        podexec[Codex executor container]
        sidecar[Egress sidecar]
      end
      subgraph vm_guest[VM guest]
        subgraph vm_runtime[Environment Runtime process]
          vmruntime[Environment lifecycle]
          guest[Runtime Session components + native drivers]
          vmexecution[Execution API]
        end
        vmruntime -->|direct lifecycle and Session calls| guest
        vmruntime -->|lifecycle and execution RPC| vmexecution
        guest_agent[Managed agent process and CLI]
        manager[systemd manager]
        vmexec[Codex executor service]
        vmruntime -->|process lifetime| manager
        guest -->|native supervision| manager
        vmexecution -->|executor supervision| manager
        manager -->|service lifetime and cgroup| vmexec
      end
      subgraph vm_sandbox[VM launcher Pod]
        relay[Egress relay]
      end
    end
    k8s[Kubernetes API]
    subgraph independent[Independent service]
      direction LR
        index[Git index]
    end
    alloy[Alloy OTLP receiver]
    sdk_services[[Instrumented Agentplane services]]
    responses[Responses API providers]
    anthropic[Anthropic Messages API]

    liveTests -->|HTTPS: operator OIDC ID token only| app
    browser -->|HTTPS, one origin| httpsGateway
    httpsGateway -->|HTML, assets and service worker| frontend
    httpsGateway -->|API, auth and streams| app
    app -->|App SVID mTLS via environments:8082: EnvironmentService diagnostics and health| sandboxctl
    app -->|App SVID mTLS via sessions:8080: Dispatcher commands, WatchSessions and archive GetSession| gw
    app -->|App SVID mTLS via environments:8082: EnvironmentService| sandboxctl
    app -->|service SVID mTLS: History reads| hs
    app -->|service SVID mTLS: operator watches and health| notif
    app -->|mTLS plus operator authority: Action and MCP Linkage RPCs| actions
    app -->|mTLS peer plus operator OIDC: ConnectionService and health| edge
    app -->|PolicyService gRPC :8083: service SVID only| policy
    edge -->|authenticated ActionService gRPC| actions
    edge -->|public MCP client metadata: gRPC| actions
    notif -->|Action history: gRPC mTLS, Notifications SVID| actions
    notif -->|Session validation + SubmitInput via sessions:8080: Notifications SVID mTLS| gw
    actions -->|sessions:8080; Actions SVID mTLS: Session commands; typed envelope for delegation| gw
    actions -->|environments:8082; EnvironmentService: Actions SVID mTLS; typed delegation for list/get/resume| sandboxctl
    actions -->|Actions SVID mTLS: Session reads; typed envelope for delegation| hs
    app -->|service SVID mTLS: admin reads| egress
    app --> electric
    app --> k8s
    sdk_services -->|OTLP/gRPC| alloy
    app -->|browser trace relay: OTLP/HTTP| alloy

    gw -->|central HarnessHost creation, commands + WatchSessions: Sessions SVID mTLS| sampler
    sampler -->|continuation, commands and ownership leases| samplerdb
    samplerdb -->|NOTIFY: central_sampler_work| sampler
    samplerdb -->|NOTIFY: central_sampler_outbox| sampler
    sampler -.->|optional Action tools: service mTLS and Session delegation, gRPC| actions
    sampler -->|Executor gRPC via environments:8082, sampler SVID mTLS| sandboxctl
    sampler -->|mTLS sampler SVID + typed Session delegation: journal push| hs
    sampler -->|Responses HTTPS: sampler SVID mTLS + typed Identity/Session delegation| llm -->|model API| responses
    sampler -->|direct Anthropic Messages API| anthropic
    gw --> k8s
    notif -->|live workload credentials, Identities, Sandboxes and send grants| k8s
    actions -->|Action Pods, launched Sandboxes and their launcher grants| k8s
    policy --> k8s
    sandboxctl --> k8s
    sandboxctl -->|Quiesce and suspend admission: service SVID mTLS to every-replica proxy| sandboxctl
    gw -->|HarnessHost creation, commands + WatchSessions via environments:8082, Sessions SVID mTLS| sandboxctl
    sandboxctl -->|proxy / Executor: runtime-proxy SVID; independent watches and fenced calls| runtime
    sandboxctl -->|proxy / Executor: runtime-proxy SVID; independent watches and fenced calls| vmruntime
    sandboxctl -->|passive WatchState binds workload UID and observes facts: mTLS gRPC| runtime
    sandboxctl -->|passive WatchState binds workload UID and observes facts: mTLS gRPC| vmruntime
    runner -->|local Codex JSON-RPC over WebSocket, isolated mode| podexec
    runner -->|starts| agent
    podexec --> sidecar
    guest -->|local Codex JSON-RPC over WebSocket, isolated mode| vmexec
    guest -->|starts| guest_agent
    vmexec --> relay
    runner -->|local HTTP CONNECT| sidecar
    sidecar -->|mTLS Runtime SVID :8888| egress
    guest -->|HTTP CONNECT through launcher relay| relay
    relay -->|mTLS Runtime SVID :8888| egress
    agent -.->|local Rules gRPC :3130| sidecar
    sidecar -.->|service SVID TLS through CONNECT| egress
    guest_agent -.->|local Rules gRPC :3130| relay
    relay -.->|service SVID TLS through CONNECT| egress
    egress -.->|opaque Action gRPC TLS, verified by the service| actions
    egress -.->|opaque Sandbox SVID TLS: EnvironmentService via environments:8082| sandboxctl
    runner -.->|OTLP/gRPC telemetry route| sidecar
    sidecar -->|OTLP/gRPC| alloy
    guest -.->|OTLP/gRPC telemetry route| relay
    relay -->|OTLP/gRPC| alloy
    runner -.->|History mTLS through CONNECT| sidecar
    sidecar -.->|opaque History TLS| egress
    egress -.->|opaque SessionJournalIngress.Push TLS| hs
    agent -.->|managed CLI History gRPC :3130| sidecar
    sidecar -.->|Sandbox SVID TLS through CONNECT; verifies History SVID| egress
    agent -.->|Session gRPC through HTTPS_PROXY/CONNECT; EnvironmentService gRPC :3130| sidecar
    guest -.->|History mTLS through CONNECT| relay
    relay -.->|opaque History TLS| egress
    guest_agent -.->|managed CLI History gRPC :3130| relay
    relay -.->|Sandbox SVID TLS through CONNECT; verifies History SVID| egress
    guest_agent -.->|Session gRPC through HTTPS_PROXY/CONNECT; EnvironmentService gRPC :3130| relay
    egress -.->|opaque Sandbox SVID TLS via sessions:8080| gw

    egress -.->|opaque TLS; Rules verifies SVID :8081| egressRules
    agent -.->|Notification HTTP| sidecar
    guest_agent -.->|Notification HTTP| relay
    sidecar -.->|Notification SVID TLS through CONNECT| egress
    relay -.->|Notification SVID TLS through CONNECT| egress
    egress -.->|opaque Notification TLS| notif
    agent -.->|local Notification gRPC :3130| sidecar
    guest_agent -.->|local Notification gRPC :3130| relay
    runner -.->|local model configuration gRPC :3130| sidecar
    guest -.->|local model configuration gRPC :3130| relay
    sidecar -.->|model configuration SVID TLS through CONNECT| egress
    relay -.->|model configuration SVID TLS through CONNECT| egress
    egress -.->|opaque model configuration TLS| llm


    click app href "../app/internal/server/server.go" "Integration app API server"
    click gw href "../session_dispatcher/dispatcher/SPEC.md" "Session Dispatcher"
    click hs href "../history_service/api/history/history_service.proto" "History Service API"
    click notif href "../notification_service/api/notifications/notifications.proto" "Notification Service API"
    click actions href "../action_service/api/contracts/action_service.proto" "Action Service API"
    click edge href "../mcp_edge/api/connection/connection_service.proto" "Connection API"
    click policy href "../policy_controller/internal/controller/controller.go" "Policy Controller"
    click sandboxctl href "../environments/internal/controller/controller.go" "Sandbox Controller"
    click egress href "../egress/internal/proxy/proxy.go" "Egress proxy"
    click llm href "../llm_ingress/server.go" "LLM ingress"
    click electric href "../../cluster/cdk8s/agentplane/electric.py" "Electric deployment"
    click index href "../x/indexing/app.py" "Git index API"
```

First-party backend calls use mounted service SVIDs and the workload trust bundle. Their Pods
do not project destination-audience ServiceAccount tokens and have no TokenReview RBAC grants.
Kubernetes API credentials remain separate.

Each runner incarnation has one Secret, issued by the Policy Controller for the Sandbox's
`WorkloadCredential`. The Sandbox Controller projects it to Environment Runtime and the egress
sidecar only; workload containers have no mount. The KubeVirt launcher relay gets that same Secret
through the existing Kyverno injection after a live check that the record has accepted the
launcher's VMI for the VM's credential. Staging
and testing use the SVID hop; TLS mode never falls back to token mode.

The Gateway uses its Sessions caller SVID (`spiffe://agentplane/sessions`) to reach the Environments
proxy and sampler. Its listener retains `spiffe://agentplane/session-dispatcher` as the server URI.
Environments owns Runtime proxy, HarnessHost, Executor and the `spiffe://agentplane/runtime-proxy` outbound SVID,
separate from the Controller's binding observer. The old Gateway Runtime certificate and unused role
mounts remain deployed only until the receiver-removal image is pinned and running; they grant no
receiver authority in that image. Sessions-to-proxy remains a network hop with its own exact grant.

Firecracker Sandboxes use a controller-owned launcher Pod, a separate SVID-authenticated relay and a
NixOS guest. Their supervisor checkpoints RAM and all disks, then restores the same Runtime and
processes in the same Pod. Its launcher serves SPIFFE Workload API credentials over vsock to the
root guest agent, which serves a restricted Unix endpoint consumed by Runtime. Guest management
uses a separate gRPC connection for boot/thaw readiness. See [Firecracker Sandboxes](firecracker_sandboxes.md) for the control,
credential and network boundaries.

A VM guest runs Environment Runtime with Session components and Execution; its relay is in the separate KubeVirt launcher Pod. The Git index is
standalone, with no dependency on the backend services or the app: it polls a configured Git remote,
embeds files through an embedding endpoint and owns its own PostgreSQL database
([indexing README](../x/indexing/README.md)).

- **Sessions and Threads.** A Session is a runner's raw Event stream; a Thread is its conversation
  view, folded by the History Service. Runners push Events to the History Service, the only way into
  retained history.
- **Runner control uses native gRPC.** Every Environments proxy replica independently dials each current
  Runtime's private server using mTLS and opens a state watch. Its calls carry the live
  Sandbox UID, accepted workload Pod UID and observed Runtime boot ID. The Sandbox Controller's
  direct mTLS `observe_only` watch binds the workload UID before the first facts snapshot; the
  Controller keeps those facts in memory per Sandbox, incarnation and boot. Proxy watches cannot
  select or replace that binding. Callers use `sandbox-controller:8082`; no Gateway
  connection Lease or per-Sandbox `<sandbox>-control` EndpointSlice decides Runtime call ownership.
  Loss of one observation does not revoke another proxy replica's calls or Runtime-owned admitted
  work. History push and ordinary Sandbox egress retain
  their sidecar or VM relay paths; History terminates and verifies the Runtime SVID itself.
- **Telemetry uses Alloy as its collector.** SDK-instrumented services export OTLP/gRPC to Alloy.
  Browser spans post to the same-origin App relay, which forwards OTLP/HTTP to Alloy. Runner spans use
  the sidecar's local telemetry route; VM guest spans use the launcher relay's guest gateway route.

### Database inventory

Each service owns a database on one shared CNPG cluster: `app`, `sandbox_service`
(Session history, owned by the History Service), `actions`, `egress`, `notifications` and
`session_dispatcher` (also the Gateway's queue; the Gateway has no registry database). The
optional central sampler owns its separate continuation database and uses PostgreSQL notifications
for resident Session work and History export wakeups.

### Sandbox lifecycle and runner control

The app forwards authenticated Sandbox management requests to the Sandbox Controller
EnvironmentService on every replica, which writes Sandbox desired state to Kubernetes. The
Gateway no longer registers EnvironmentService. The elected Controller
reconciles desired state, owns `Sandbox.status`, and holds
deletion until the runner has sealed its Sessions through the Environments proxy. Notifications and the
Runner Gateway read Sandbox inventory directly. The Runner Gateway implements Dispatcher Session
openings and durable command admission/delivery; the app reads live
runner inventory and diagnostics through the Runner Proxy, which checks the live Sandbox name, UID
and owner. The Runner starts and resumes its own harnesses.

## Focused diagrams

Per-path detail for the [system overview above](#system-overview), using the same conventions:
API arrows point caller to callee, labels like `response` or `replication` show data on that edge,
dashed arrows are runner-initiated platform routes.

The optional central sampler runs as its own Deployment and Service in staging and the
[k3s acceptance scenarios](../system_tests/README.md#central-sampler), using the shared cdk8s
construct and its own image. Responses models call LLM ingress with the sampler service SVID over
mTLS and typed execution Identity/Session delegation; selected Anthropic API models use a dedicated
mounted key and a host-pinned Cilium egress rule.

## Session delivery

```mermaid
flowchart LR
    app[Integration app]
    actions[Action Service]
    history[History Service]
    subgraph unified[Runner Gateway]
        dispatcher[Dispatcher API and durable delivery]
        rpc[Shared TLS RPC listener :8080<br/>service and workload Identity SVID auth]
    end
    pod[Managed Pod agent process and CLI]
    sidecar[Egress sidecar]
    guest[Managed VM agent process and CLI]
    relay[VM launcher relay]
    egress[Egress proxy]
    central[Sampler]
    sandboxctl[Environments :8082: EnvironmentService, Runtime proxy, HarnessHost and Executor]
    k8s[Kubernetes API]
    app -->|Session creation + commands| dispatcher
    app -->|plain browser proxy: Dispatch and Session watches; archive GetSession: gRPC| dispatcher
    app -->|EnvironmentService exact reader/manager/operator roles: App SVID mTLS| sandboxctl
    actions -->|environments:8082; caller-delegated EnvironmentService list/get/resume; reviewed launches: Actions SVID mTLS| sandboxctl
    actions -->|sessions:8080; Actions SVID mTLS: Session commands; typed envelope for delegation| dispatcher
    actions -->|Actions SVID mTLS: Session reads; typed envelope for delegation| history
    app -->|EnvironmentService: inventory + on-demand diagnostics| sandboxctl
    dispatcher -->|Sessions SVID mTLS: HarnessHost creation, commands + WatchSessions| sandboxctl
    dispatcher -->|central HarnessHost creation, commands + WatchSessions: Sessions SVID mTLS :8083| central
    sandboxctl -->|every-replica inventory and guarded desired-state writes| k8s
    sandboxctl -->|Controller SVID mTLS: Quiesce via every-replica proxy| sandboxctl
    sandboxctl -->|every-replica proxy: runtime-proxy SVID; independent watches and fenced calls| runtime
    sandboxctl -->|passive WatchState binds workload UID and observes facts: mTLS gRPC| runtime
    dispatcher -->|Sandbox + independent environment-policy watches| k8s
    dispatcher -->|automatic wake: budget + guarded running/generation update| k8s
    pod -.->|Session gRPC over HTTPS_PROXY/CONNECT; EnvironmentService gRPC :3130| sidecar
    guest -.->|Session gRPC over HTTPS_PROXY/CONNECT; EnvironmentService gRPC :3130| relay
    sidecar -.->|mTLS proxy hop and CONNECT| egress
    relay -.->|mTLS proxy hop and CONNECT| egress
    egress -.->|Opaque Runner SVID TLS to sessions:8080| rpc
    egress -.->|Sandbox SVID TLS to environments:8082| sandboxctl
    rpc -->|Method-authorized Session calls| dispatcher
```

Managed Pod and guest callers receive `AGENTPLANE_SESSIONS_URL` at
`sessions.<namespace>.svc.cluster.local:8080`. The egress sidecar or launcher relay maps that
origin through central CONNECT and presents its Runner SVID to the Sessions listener. The central
proxy carries the destination TLS bytes opaquely. Sessions resolves the live workload credential and exact Identity UID
before applying the Session method rules. No Dispatcher audience token is projected
into Sandbox Pods. Backend checks of the Sandbox identity and
object UID remain authoritative; network reachability grants no Session authority. The Action Service
presents its service SVID on Dispatcher, History and EnvironmentService calls, including the opted-in
`EnvironmentService` list/get methods and its separately configured
`EnvironmentService.ResumeEnvironment` method. The receiver applies the caller identity from the
shared delegation envelope, then checks the current Sandbox and explicit Identity resume grant.
The lifecycle consumer watches PolicySets, PolicyBindings and Identities through its typed policy index;
a failed policy watch refuses explicit Identity resume while implied self-reads remain available.
Dispatcher constructs its own environment-policy index and watches for native Identity `CreateSession`
with `bind_session`; it records the placed Sandbox's exact live Identity as the Session principal,
and retains the effective creator and actor separately. These consumers share an evaluator library,
not an in-process index. The integration app uses its own Pod-bound
token as a distinct operator broker; it does not forward browser identity. Resume acceptance is not
readiness, and no resume path replays Session input or creates a grant.
Explicit resume is internal to Environments and accepts no wake budget. Dispatcher owns automatic
wake execution and PostgreSQL budgeting, preserving row attribution and applying `wakeOnSend` plus
the retained caller's policy or a configured service-broker check. An Action-attributed row uses the
Actions service actor's Dispatcher broker grant while retaining its effective Identity. Both owners
use the same `environments/lifecycle`
authorization and guarded-write code with independent policy views; Environments does not call
Sessions. Conflicts reread and reauthorize, while an uncertain patch outcome is never replayed.
The opt-in Session tools also forward the effective caller to Dispatcher commands and retained
History reads. Those receivers enforce their own same-principal checks; the existing
`orchestration_v1` broker path retains its separate authority.

Managed Pod, VM and Firecracker CLIs also receive `AGENTPLANE_HISTORY_URL`, set to the shared local
platform gRPC listener at `http://<local gateway>:3130`. Its History method route connects to
`history-service.<namespace>.svc.cluster.local:8080` with TLS. The sidecar presents the Sandbox
Workload SVID and verifies `HISTORY_SERVICE_URI`; the central proxy carries that TLS connection
opaquely. History authorizes the read against the exact Identity name and UID in the SVID. This path
uses no History bearer credential or projected audience token and adds no `reader_accounts` grant.

### Service admission in the current Gateway

Gateway admits exact service SVIDs for Dispatcher component calls. Environments grants Runtime
proxy and Executor methods separately.
Dispatcher also admits live workload Identity SVIDs and configured service delegation.
EnvironmentService has its own receiver and separate reader, manager, launcher and operator grants.
App and Notifications have exact service-SVID automatic wake-broker grants, separately from
explicit operator resume. Dispatcher also configures Actions as a service wake broker for rows
carrying its Action proof. The Environments service has a separate wake-caller list for its own
assessments. Notification wake remains conditional on the target's wake-on-send policy.
When `orchestration_v1` is configured, the exact `spiffe://agentplane/actions` SVID also receives
Dispatcher component authority and EnvironmentService launcher authority. Both grants follow the
existing `actions.orchestration_callers` map; session delegation alone does not enable them. The
launcher handler allows template listing and environment creation with a complete Action launch
origin, while the Dispatcher role remains separate from operator authority. History's exact reader
SVID list includes Actions. Actions presents that SVID for both direct orchestration and
caller-delegated Dispatcher/History requests. Delegated calls retain their typed Action/Identity
envelope; direct component grants never supply the effective caller's authority. Gateway receivers reject Kubernetes bearers; service-token projections and TokenReview RBAC are absent. History's read
and journal paths admit workload or exact service SVIDs only.

```mermaid
flowchart LR
    app[App service] -->|service mTLS: read, manage and direct create grants| environments[EnvironmentService]
    actions[Action service] -->|sessions:8080; direct service mTLS: orchestration Dispatcher role| dispatcher[Dispatcher / Sessions role]
    actions -->|sessions:8080; service mTLS + typed Identity envelope: session delegation| dispatcher
    actions -->|service mTLS: direct orchestration reads or typed Identity envelope| history[History]
    actions -->|environments:8082; direct service mTLS: orchestration launcher role| environments
    actions -->|environments:8082; service mTLS + typed Identity envelope: session delegation| environments
    sessions[Sessions service role] -->|service mTLS: Runtime Session methods| proxy[Runtime proxy role]
    controller[Sandbox Controller service] -->|service mTLS: controller methods| proxy
    sampler[Central sampler service] -->|service mTLS: Executor grant| executor[Executor]
    proxy -->|runtime-proxy SVID, fenced mTLS| runtime[Native Runtime]
```

## Central code-mode sampling (opt in)

```mermaid
flowchart LR
    dispatcher[Gateway Dispatcher]
    sampler[Central sampler and shared Codex host]
    db[(Sampler PostgreSQL)]
    proxy[Environments Executor :8082]
    runtime[Environment Runtime]
    execution[Execution Service]
    executor[Loopback Codex exec-server]
    history[History ingress and Thread fold]
    llm[LLM ingress]
    responses[Responses API providers]
    anthropic[Anthropic Messages API]
    dispatcher -->|HarnessHost gRPC :8083; Sessions service SVID mTLS| sampler
    sampler -->|continuation, commands and ownership leases| db
    db -->|NOTIFY: central_sampler_work| sampler
    db -->|NOTIFY: central_sampler_outbox| sampler
    sampler -->|Executor RPCs: mTLS, sampler SVID| proxy
    proxy -->|mTLS Executor gRPC| runtime
    runtime -->|direct Executor calls| execution
    execution -->|local Codex JSON-RPC over WebSocket| executor
    sampler -->|mTLS sampler SVID + typed Session delegation: acknowledged journal outbox| history
    sampler -->|Responses HTTPS: sampler SVID mTLS + typed Identity/Session delegation| llm -->|model API| responses
    sampler -->|Anthropic Messages API| anthropic
```

The sampler's TLS listener trusts the workload bundle and admits exactly
`spiffe://agentplane/sessions`, the owner stored by the explicit Gateway-account Session rewrite.
The Gateway authenticates with its mounted Sessions role certificate and verifies the sampler server SVID.
Sampler application RPCs require the admitted Sessions service SVID. Bearer settings no longer
grant access. Application RPCs use only the TLS listener; port 8081 serves health checks only.

The staging picker gives direct Anthropic models only to the central sampler, keeping them out of the
subscription-backed Codex and Claude harnesses. The Anthropic key is copied by ESO from the existing
LiteLLM-owned secret and mounted only in the sampler container; Cilium pins direct API traffic to
`api.anthropic.com` and its TLS server name. Responses calls use the LLM ingress HTTPS endpoint with
the sampler's service SVID over mTLS and typed Identity/Session delegation. The ingress authorizes
the recorded effective Identity; the sampler certificate supplies only the actor proof.

The sampler lazily starts one shared Codex host process when code-mode work needs it and multiplexes
logical Sessions through it. The host retains per-Session `store()` / `load()` values across cells
and idle turns; each code-mode cell runs in a fresh V8 isolate with no shared JavaScript globals.
Provider continuation stays in the sampler. A resident replica keeps the Session's database
ownership lease and a lightweight driver/heartbeat while idle; this does not consume a
`max_live_sessions` active-execution slot. Any replica may admit commands, and a transactional,
payloadless `central_sampler_work` notification wakes local lease holders to query pending work.
The separate `central_sampler_outbox` notification wakes History export. Notifications carry no
Session or Pod routing, and startup scans, listener reconnect catch-up and periodic reconciliation
cover missed signals.
The Pod has aggregate CPU/memory limits, with no per-Session or per-cell kernel boundary today.
Environments proxies the sandbox-owned [Executor service](../env/runtime/execution/executor.proto) through Environment Runtime to Execution Service,
whose execution adapter owns the local
Codex JSON-RPC connection and native session handshake. A held `OpenSession` stream owns that
connection; process and file operations use unary RPCs. Native frames return as evidence for the
sampler's existing journal. No new persistence or operation receipts are introduced, and a lost
reply can still leave an operation outcome unknown. Dispatcher is absent from that tool path. Sandbox UID and incarnation fence the executor binding; v1 does
not replace an executor. An unavailable executor does not remove the sampler's persisted history
or prevent its control RPCs. Existing Dispatcher routing still requires the bound Sandbox object.

History retains source evidence and derives the lossy Thread display. Its entries are not a
provider continuation format: the sampler owns that state in its database. Wrapped native
harnesses keep their current local sampling, execution and persistence paths. See the
[standalone API and recovery contract](../sampler/SPEC.md).

### Sampler service admission

The sampler admits only explicitly configured Sessions service SVIDs and performs no TokenReview.
Service ownership is stored as the real SPIFFE ID, separately from ServiceAccount UID ownership.
The explicit Gateway-account ownership rewrite preserves the retained Session IDs and History
references before the caller switch. The Gateway uses the same Sessions SPIFFE ID as the new owner.

```mermaid
flowchart LR
    sessions[Dispatcher / future Sessions service SVID] -->|mTLS: HarnessHost method grant| sampler[Sampler: SVID only]
    bearer[Kubernetes bearer] -.->|denied before handler| sampler
    sampler -->|service SPIFFE ID ownership| db[(Sampler PostgreSQL)]
```

### LLM ingress certificate admission

Exact service SVID allowlists cover ModelConfig reads and the Central Sampler's typed inference
calls. A sampler actor must also prove the effective live Identity and Session in the delegation
envelope. Workload calls resolve the live credential's Identity; inference and ModelConfig never
admit Kubernetes bearers. A failed certificate cannot fall back to a token.

```mermaid
flowchart LR
    workload[Workload Identity SVID] -->|mTLS: live credential verification| llm[LLM ingress]
    readers[Configured service SVID readers] -->|mTLS: ModelConfig read methods| llm
    sampler[Sampler service SVID] -->|mTLS plus typed Identity and Session delegation| llm
    llm -->|effective Identity egress grants| provider[Model provider]
```

## Environment control and native Sessions

The Environments Runtime proxy, HarnessHost and Executor admit only exact component service SVIDs: configured Runtime
readers and Sessions for HarnessHost session inventory reads, Sessions for native Session creation, commands and
watches, Sandbox Controller for lifecycle operations, and Central Sampler for Executor methods. HarnessHost inventory
admits the union of `session_callers` and `readers`; existing EnvironmentRuntime diagnostics retain their configured
reader grants.
These are independent method grants. Caller configuration activates each edge only after its image
is pinned; the proxy's existing native Runtime mTLS identity remains separate.

```mermaid
flowchart LR
    app[App service SVID] -->|EnvironmentService.GetRuntimeDiagnostics only| sandboxctl
    services[Configured Notifications, Sessions, Controller and sampler service SVIDs]
    services -->|service SVID only: method-scoped Runtime, HarnessHost and Executor grants| sandboxctl
    subgraph environment_runtime[Environment Runtime process]
      runtime[Runtime admission and lifecycle]
      sessions[Session manager, adapters and journals]
      execution[Execution API]
      bootstrap[Bootstrap]
      delivery[History delivery worker]
      runtime -->|construct and own| sessions & execution & bootstrap & delivery
      sessions -->|journal inventory| delivery
    end
    history[History ingress]
    sandboxctl -->|lifecycle + diagnostics / HarnessHost / Executor: runtime-proxy SVID; independent watches and fenced calls| runtime
    policy_api[Kubernetes PolicySets / PolicyBindings / Identities]
    sandboxctl -->|list/watch grants; patch EnvironmentReady| policy_api
    sandboxctl[Environments :8082: EnvironmentService, Runtime proxy, HarnessHost and Executor] -->|passive WatchState binds workload UID and observes facts: mTLS gRPC| runtime
    delivery -.->|durable journal push and ACK| history
```

Runtime owns the components and one desired lifecycle revision. Native Session admission rechecks
that accepted revision and the fenced caller identity before durable work. Component observations
prove native cleanup; Execution fencing precedes journal sealing, and exact durable History ACKs
precede delivery/journal closure and owner release. Calls use the immutable Controller-bound
Sandbox and incarnation identity plus the Runtime boot ID. Central sampler remains
a separate control-plane process.

## Codex execution inside a Sandbox

```mermaid
flowchart LR
    subgraph pod[Sandbox Pod]
        subgraph pod_runtime[Environment Runtime process: PID1]
          runtime[Environment lifecycle]
          host[Session and harness components]
          execution[Execution API]
        end
        exec[Companion container and exec-server]
        runtime -->|construct and own| host
        runtime -->|component lifecycle| execution
        execution -->|ownership gRPC over Unix socket| exec
        host -->|shared app-server WebSocket tools| exec
    end
    subgraph vm[VM guest]
        subgraph vm_runtime[Environment Runtime service]
          vmruntime[Environment lifecycle]
          vmhost[Session and harness components]
          vmexecution[Execution API]
        end
        manager[systemd manager]
        harness[Codex app-server: workload slice]
        vmexec[exec-server: executor slice]
        vmruntime -->|service lifetime| manager
        vmhost -->|native launch / fence| manager
        vmexecution -->|executor launch / fence| manager
        manager -->|service lifetime and cgroup| vmexec
        manager -->|service lifetime and cgroup| harness
        harness -->|loopback WebSocket tools| vmexec
    end
```

The Pod executor has a private PID namespace and Kubernetes resource limits. VM native processes use
separate service scopes and slice budgets. Both share Sandbox identity and filesystem paths with the
harness. Runtime aggregates process fence proof; Host cannot restart or fence Execution. An abnormal
Runtime exit requires the outer container fence before replacement. Local tools remain a deployment
mode. See [executor isolation](../env/runtime/execution/docs/codex_executor.md).

## Telemetry

```mermaid
flowchart LR
    browser([Browser])
    app[Integration App and OTLP relay]
    services[[Instrumented Agentplane services]]
    alloy[Alloy OTLP receiver]
    subgraph sandbox[Sandbox Pod]
        runner[Runner]
        sidecar[Egress sidecar]
    end
    subgraph vm_guest[VM guest]
        guest[Guest runner]
    end
    subgraph vm_pod[VM launcher Pod]
        relay[Egress relay]
    end

    browser -->|same-origin POST /telemetry/v1/traces| app
    app -->|OTLP/HTTP :4318| alloy
    services -->|OTLP/gRPC :4317| alloy
    runner -.->|OTLP/gRPC 127.0.0.1:3130| sidecar
    sidecar -->|OTLP/gRPC :4317| alloy
    guest -.->|OTLP/gRPC 10.0.2.1:3130| relay
    relay -->|OTLP/gRPC :4317| alloy
```

The browser exports protobuf traces to the same-origin App endpoint. The App fixes the upstream
receiver from its server-side configuration and forwards the bounded request to Alloy over OTLP/HTTP;
the browser cannot choose a destination. Instrumented backend services export OTLP/gRPC directly to
Alloy. Runners use an unauthenticated local gRPC route in their egress sidecar; VM guests use the
launcher relay at the guest gateway. Both relays forward to Alloy, and the sidecar and relay export
their own traces directly there as well.

## History and browser sync

```mermaid
flowchart LR
    browser([Browser])
    frontend[Static frontend]
    app[Integration app]
    history[History Service]
    dispatcher[Session Dispatcher]
    probe[Opt-in packing probe]
    history_db[(History PostgreSQL)]
    electric[Electric]
    subgraph pod[Sandbox Pod]
        runner[Runner]
        sidecar[Egress sidecar]
    end
    subgraph vm_guest[VM guest]
        guest[Guest runner]
    end
    subgraph vm_pod[VM launcher Pod]
        relay[Egress relay]
    end

    browser -->|HTML, assets and service worker| frontend
    browser -->|operator OIDC cookie: API + shape requests, same origin| app
    browser -->|generated ProtoJSON History reads and summary watch| app
    browser -->|Dispatcher Session inventory JSON facade: protobuf-es| app
    app -->|App SVID mTLS: WatchSessions per browser stream| dispatcher
    app -->|gRPC reads and watch| history
    app -->|proxied shape request| electric
    electric -->|shape response data| app
    app -->|API + SSE + shape data| browser
    runner -.->|opens SessionJournalIngress.Push| sidecar
    sidecar -.->|outbound platform route| history
    guest -.->|opens SessionJournalIngress.Push| relay
    relay -.->|outbound platform route| history
    probe -->|sampler SVID journal push + reader service SVID reads: mTLS| history
    history -->|archive and fold reads / writes| history_db
    history_db -->|logical replication data| electric
```

The App authorizes the Session inventory stream at its operator-only browser login boundary and
ends it on logout or expiry. The proxied Dispatcher watch supplies membership and materialization;
App metadata supplies titles/archived state, and History supplies conversation and existing activity
observations. No browser connects directly to Dispatcher; there is no ListSessions bootstrap.

The app initiates each Electric HTTP request and streams the shape response through to the browser.
Generated History HTTP routes at the app's `/upstream/history/v1/` prefix let the browser call
annotated History reads and summary watches; the app forwards them over gRPC with its service SVID
over mTLS. The browser joins these with Dispatcher inventory and App-owned titles/archived state;
there is no App History relay or membership cache.
The separate `History PostgreSQL → Electric → app → browser` arrows show replicated and returned data.
The HTTPS Gateway routes static files directly to the frontend Service and all API, login and stream
requests to the app Service. The frontend has no backend credentials and makes no outbound service
calls; its History reads use the app's same-origin facade.

## Notifications

```mermaid
flowchart LR
    agent([Agent])
    app[Integration app]
    github([GitHub])
    notifications[Notification service]
    actions[Action Service]
    k8s[Kubernetes API]
    dispatcher[Runner Gateway Dispatcher API]
    history[History Service]

    sidecar[Egress sidecar or launcher relay]
    proxy[Central egress proxy]
    agent -.->|HTTP subscriptions, inboxes, messages| sidecar
    sidecar -.->|Sandbox SVID to destination inside CONNECT| proxy
    proxy -.->|opaque TLS| notifications
    app -->|gRPC diagnostics + health; exact App service SVID| notifications
    github --> notifications
    notifications --> github
    notifications -->|Action history: gRPC mTLS, Notifications SVID| actions
    notifications -->|live workload credentials, Identities, Sandboxes and send grants| k8s
    notifications -->|Session validation + SubmitInput via sessions:8080: Notifications SVID mTLS| dispatcher
    notifications -->|command receipts: mTLS, Notifications SVID| history
```

Sandboxes keep their existing HTTP service URL. Their sidecar or relay recognizes the exact origin,
opens CONNECT through the central proxy, and presents the Runner SVID to Notification's shared TLS
RPC listener. Notification resolves the credential's current Identity name and UID and preserves the
existing Session and Sandbox ownership checks. The receiver rejects Kubernetes bearer credentials and
performs no TokenReview. It accepts the exact App service SVID only for the five read-only Session
diagnostic methods. That service grant never supplies an agent Identity or permits inbox mutation.

The Notification service does not call the integration app, which watches subscription and notice
state over the diagnostic methods of the `Notifications` gRPC service. GitHub sends it signed webhooks; it
makes GitHub App REST calls, reads receipt evidence from History before retrying a delivery, and
submits every runner notice through the Session Dispatcher. It authorizes a message against the
PolicySets and PolicyBindings it watches, and asks History which Sandbox wrote a Session before a
message opens that Session's inbox.

## Actions and egress

```mermaid
flowchart LR
    mcp_client([MCP client])
    github([GitHub / web-push])
    app[Integration app]
    actions[Action Service]
    edge["MCP edge<br/>mTLS gRPC 8443 + HTTP 8081<br/>/v1 handler in process"]
    k8s[Kubernetes API]
    subgraph box[Action box Pod]
        workload[Box workload]
        box_sidecar[Egress sidecar]
    end
    subgraph sandbox[Sandbox Pod]
        runner[Runner]
        sidecar[Egress sidecar]
    end
    subgraph vm_guest[VM guest]
        guest[Guest runner]
    end
    subgraph vm_pod[VM launcher Pod]
        relay[Egress relay]
    end
    egress[Egress proxy]
    llm[LLM ingress]
    litellm[LiteLLM]
    other((Other endpoints))
    mcp_servers((MCP servers))

    mcp_client -->|MCP over OAuth| edge
    app -->|Action RPCs: service SVID mTLS + operator OIDC; MCP OAuth callback: HTTP| actions
    app -->|ConnectionService RPCs and health: mTLS gRPC :8443| edge
    edge -->|mTLS: edge SVID + typed Connection delegation| actions
    edge -->|public MCP client metadata: gRPC| actions
    actions -->|create + watch Pods| k8s
    actions -->|exec via Kubernetes API| workload
    workload --> box_sidecar
    runner --> sidecar -->|mTLS Runtime SVID :8888| egress
    guest --> relay -->|mTLS Runtime SVID :8888| egress
    nonSandbox[Action boxes] -->|mTLS box SVID :8888| egress
    standing[Haku UI and Anki sidecars] -->|mTLS standing SVID :8888| egress
    app -->|EgressAdmin mTLS gRPC :8081; App SVID| egress
    egress -->|policy and credential watches; live Sandbox lookup for SVID| k8s
    egress -->|policy-approved requests| other
    egress -.->|opaque TLS; Rules verifies SVID :8081| rules[Rules and EgressAdmin TLS gRPC :8081]
    egress -.->|opaque TLS to shared SVID RPC listener :8080| llm
    sampler -->|HTTPS mTLS :8080; sampler SVID + typed Identity/Session delegation| llm
    llm -->|model API + server-held key| litellm
    actions -->|MCP calls| mcp_servers
    actions -->|REST + web push| github
```

The required mTLS hop resolves the SVID's live `WorkloadCredential` before using its subject
ServiceAccount as the policy subject; no bearer authenticates it. Rules uses that same
authentication policy
and terminates TLS through an opaque central tunnel;
the proxy never asserts the caller. History, Notification, Gateway and Action retain their
method-specific authorization while their receiver retirements roll out. LLM ingress authenticates
workload and service SVIDs on its primary TLS listener, with no bearer admission. With `service_callers` configured, the central sampler's TLS listener
presents its service SVID and admits exact service peers. Its History exporter presents the
sampler SVID with typed Session delegation for the effective Identity. Only LLM ingress holds the LiteLLM key.
Kubernetes retains its destination-scoped projected token until the later impersonation cutover.
Rules, Notification, ModelConfig and telemetry share the local gRPC listener on port 3130; exact
service prefixes select independent upstream connections and authentication.

EgressAdmin admits only the exact configured service identities over mTLS. The App presents
`spiffe://agentplane/app`; bearer credentials cannot authenticate its admin reads.

Action command/build boxes present their own `WorkloadCredential`'s SVID, subject their caller's
ServiceAccount; Haku UI/Anki present their standing records' SVIDs, subject `haku-ui`. Their
sidecars alone hold credentials. The later shared
identity and SVID lifecycle is independent of Sandboxes; no Identity/account cutover is included
here. Existing Sandbox processes need recreation to read the new mode/route configuration; image
updates alone do not replace running Pods.
The app has independent `action_federation.service_url` and `connection_service_url` settings.
Both tiers send operator Connection calls and gRPC health probes to the [MCP edge](../mcp_edge/README.md).
Staging's external MCP/OAuth routes terminate there too, preserving the public issuer/resource URLs.
Actions retains upstream MCP linkage and its public client metadata. The edge verifies operator OIDC separately from
external MCP bearers, then calls Actions with its exact service SVID and typed Connection/grant provenance. It sends no
Action audience bearer and has no bearer fallback. Its gRPC listener on port 8443 checks the App
service SVID against the workload CA; operator OIDC remains required for App's operator methods and
is separate from the edge's Connection delegation. The App uses that TLS listener.

## Action Service certificate callers

The Actions receiver accepts exact App and Notifications service SVID grants and live workload
Identity SVIDs. Kubernetes TokenReview bearers grant no admission. App operator calls also require
independently verified operator OIDC; Notifications receives read-only request access. MCP edge
presents its exact service SVID with typed Connection delegation, and Session delegation likewise
requires its configured service actor. Delegation keeps the service actor separate from the effective
Identity, whose grants and live UID govern the call. Public OAuth and client metadata endpoints retain
their method-specific authentication exceptions.

```mermaid
flowchart LR
    app[App service SVID] -->|mTLS plus operator OIDC| actions[Action Service]
    notifications[Notifications service SVID] -->|mTLS: read methods only| actions
    mcp[MCP edge service SVID] -->|mTLS + typed Connection delegation| actions
    session[Configured Session service actor] -->|mTLS + typed Session delegation| actions
    workload[Live workload Identity SVID] -->|mTLS: Identity grants| actions
    actions -->|effective Identity grants and live UID| policy[Policy bindings]
```

## Service definitions

- gRPC: [Actions](../action_service/api/contracts/action_service.proto), [operator Actions](../action_service/api/contracts/operator_action_service.proto), [Connections](../mcp_edge/api/connection/connection_service.proto), [Web Push](../action_service/api/web_push/web_push_service.proto), [Environment Service](../environments/v1/environment_service.proto), [Environment Runtime](../env/runtime/control/environment_runtime.proto), [HarnessHost](../harness_host/harness_host.proto), [Executor](../env/runtime/execution/executor.proto), [SessionJournalIngress](../history_service/api/ingress/session_ingress.proto), [Session Dispatcher](../session_dispatcher/dispatcher/api/session/session_dispatcher.proto), [History Service](../history_service/api/history/history_service.proto), and the [Notification Service](../notification_service/api/notifications/notifications.proto) API (whose HTTP annotations generate the App browser facade; the service itself serves workload calls over gRPC). The Runner Gateway serves Dispatcher and standard gRPC health; Environments proxies the Environment Runtime, HarnessHost and Executor services over direct mTLS connections.
- HTTP and streaming APIs: [Integration app](../app/) (Bazel target: `//agentplane/app/cmd:agentplane-app`), [Electric proxy](../app/internal/proxy/electric.go), [Action Service generated facade](../action_service/api/contracts/action_service.proto), [LLM ingress](../llm_ingress/server.go), and [Git index](../x/indexing/app.py).
- Controllers and egress: [Sandbox Controller](../environments/internal/controller/controller.go), [Policy Controller](../policy_controller/internal/controller/controller.go), [central egress proxy](../egress/internal/proxy/proxy.go), [Go sidecar](../egress/sidecar/main.go), and [sidecar route configuration](../egress/platform_routes.py).

## Staged Runtime proxy caller identity

Runtime admits the exact `spiffe://agentplane/runtime-proxy` certificate for its existing Gateway
control and Executor capabilities, retaining the current `runner-gateway` certificate during
migration. Sessions must call through the proxy; its certificate has no direct Runtime grant.
Controller passive watches retain their separate exact identity and permissions. Runtime acceptance
of the old Gateway SVID retires after the Gateway image without direct Runtime calls has rolled.

```mermaid
flowchart LR
    sessions[Sessions service SVID] -->|separate network hop and grant| proxy[Runtime proxy]
    proxy -->|mTLS: runtime-proxy SVID| runtime[Environment Runtime]
    controller[Sandbox Controller SVID] -->|mTLS: passive WatchState only| runtime
```

## Policy and Egress Admin certificate admission

The Policy TLS listener verifies client SVIDs against the workload CA and grants the exact App SVID
separate reader and writer authority. App→Policy presents the App SVID over verified mTLS.
PolicyService accepts only service SVIDs; bearers, workload SVIDs and delegation envelopes are refused.
Egress Admin admits only configured service SVIDs, and the App uses its SVID over mTLS. The forward proxy continues
to authenticate workload Identities independently of its admin API.

```mermaid
flowchart LR
    app[App service] -->|App SVID only: reader and writer grants| policy[Policy Service]
    app -->|App SVID mTLS only: admin read methods| admin[Egress Admin]
    workload[Workload Identity SVID] -.->|existing workload authentication| proxy[Egress forward proxy]
```

## Staged Dispatcher and EnvironmentService service grants

The shared Gateway listener admits the exact App and Notifications service SVIDs to the
Dispatcher, and Actions when `orchestration_v1` grants it the component role. Its live workload
Identity SVID path and typed service delegation remain; Kubernetes
bearers grant no admission. The App SVID separately has EnvironmentService
inventory and management grants. Staging explicitly grants the App SVID resume and automatic
wake authority; these grants are independent of delegation and direct Dispatcher admission.
Actions has its own Dispatcher automatic-wake broker grant for Action-attributed rows. The
Environments service's wake assessment list remains separate. Actions service grants and delegated
Resume require their own reviewed configuration.

```mermaid
flowchart LR
    app[App SVID] -->|service SVID only: component grant| dispatcher[Dispatcher]
    notifications[Notifications SVID] -->|service SVID only: component grant| dispatcher
    app -->|separate inventory, management, resume grants| environments[EnvironmentService]
```

## Actions service delegation to explicit environment resume

The EnvironmentService receiver can admit an explicitly configured Actions service SVID with
the typed Action delegation envelope for `ResumeEnvironment`, in addition to its four delegated
read methods. The effective Identity's current environment resume grant remains the authority;
the actor certificate never grants operator authority or waives `resume_token`. Environments keeps
its separate wake assessment list; Dispatcher Action-attributed rows use its Actions service broker
grant. This receiver capability requires
a pinned image before configuration grants it to Actions; caller activation follows separately.

```mermaid
flowchart LR
    actions[Actions] -->|environments:8082; service SVID plus typed Action delegation| env[EnvironmentService]
    env -->|effective Identity resume grant and token| lifecycle[Guarded lifecycle update]
```

## Sessions role to the Runtime proxy

The Dispatcher delivery worker presents its separate `spiffe://agentplane/sessions` certificate
when dialing `sandbox-controller:8082`, independently of which proxy replica watches the
Runtime. It verifies the receiver's `spiffe://agentplane/sandbox-controller` server certificate. The receiver grants HarnessHost creation, commands and current-state watches to that role. The Dispatcher uses the same HarnessHost client for hosted and central delivery.
The Gateway→central sampler hop also presents the Sessions certificate, following the explicit
account-to-SVID ownership rewrite. A rollback to bearer requires the reviewed reverse ownership Job.

```mermaid
flowchart LR
    sessions[Dispatcher / Sessions role] -->|service SVID mTLS; Runtime Session methods| proxy[Runtime proxy]
    sessions -->|Sessions SVID mTLS; rewritten ownership| sampler[Central sampler]
    proxy -->|existing Runtime mTLS| runtime[Environment Runtime]
```

## Environments Runtime transport receiver

Deployment enables `environment_service.runtime_proxy` on the existing every-replica
`sandbox-controller:8082` listener, registering Runtime proxy, HarnessHost and Executor with exact service SVID
grants. Each replica's routing inventory and Runtime watches operate independently of leadership
and the leader's lifecycle facts. The dedicated outbound `runtime-proxy` SVID remains separate
from the Controller's incarnation-binding observer; UID/incarnation/boot fencing and uncertain
mutation outcomes stay unchanged. Notifications inventory, legacy Runner Gateway delivery and
Controller lifecycle target this Service. The Sessions Deployment uses `environments:8082` for
HarnessHost RPCs, and the central sampler uses it for Executor RPCs. Both Services use the same
Sandbox Controller server SVID.
App diagnostics use EnvironmentService on the same listener, without a raw-proxy grant. Dispatcher API calls retain their separate endpoint and identity.
The Gateway registers no Runtime proxy, HarnessHost or Executor receiver. Its obsolete credentials, network
permissions and ignored grant settings retire after the receiver-free image is pinned and running.
No Environments-to-Sessions edge is added.
