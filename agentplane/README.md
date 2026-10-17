# Agentplane

Agentplane runs coding agents in Kubernetes sandboxes and mediates what they do outside them:
network egress with credentials substituted at the proxy, operator-approved or policy-approved
Actions, and notifications. Its services are separately deployable; the Action Service alone is
an MCP approval gateway for any MCP-capable LLM product.

**Design principles:** [`docs/principles.md`](docs/principles.md). Check new designs against it.

## Services

- [Action Service](action_service/README.md): ActionRequests, Decisions, auto-approval policies,
  and an MCP endpoint over OAuth.
- [Runner Gateway](runner_gateway/README.md): one process serving the runner control stream,
  authorized live Runner proxy and [Session Dispatcher](session_dispatcher/README.md) durable queue.
  Serving waits for a root-owned queue migration ([deployment](runner_gateway/README.md#deployment)).
- [History Service](history_service/README.md): retained Session history, read by the
  integration app and the Notification service.
- [Runner](runner/README.md) and [native drivers](native/README.md): run Claude Code or Codex in a
  sandbox and journal what they do.
- [Egress proxy](egress/README.md): authenticates sandbox workloads and substitutes credentials.
- [Policy Controller](policy_controller/README.md): expires `PolicyBinding`s and keeps the
  RoleBindings they grant. Runs on staging and `agentplane-testing`; only Flux writes `PolicyBinding`s yet.
- [Sandbox Controller](sandbox_controller/README.md): reconciles `Sandbox` into its ServiceAccount,
  `PolicyBinding`, state PVC and Pods or a KubeVirt VM (VMs on staging and `agentplane-testing`).
- [LLM ingress](llm_ingress/README.md): authenticated hop in front of LiteLLM.
- [Notification service](notification_service/README.md): subscriptions and session inboxes.
- [Indexing](indexing/README.md): Git semantic search.
- [Integration app](app/README.md): the human-facing frontend over the services above, and a
  client of them.

## Go service images

The Go relay, LLM ingress, Runner Gateway, queue migrator, Policy Controller and Sandbox
Controller use the pinned `gcr.io/distroless/static-debian13:nonroot` base. Their binaries are
built with `pure = "on"`; images retain explicit UID/GID `1000:1000` and working directory `/`.
The base supplies CA certificates and timezone data. LLM ingress retains the repository's
explicit CA bundle layer. These images have no shell; runners and Python services use their
own runtime images.

## System overview

These diagrams show service boundaries and traffic, independent of any one environment or replica
layout. The overview keeps the complete service map compact; the focused diagrams below show the
API callers and data paths. API arrows point from caller to callee; labels such as `response` and
`replication` show data moving along that edge. Dashed arrows are runner-initiated platform routes.
The Git index is independent of the backend service graph and has no dependency on the integration app.

### Overview

```mermaid
flowchart LR
    browser([Browser])
    app[Integration app]
    subgraph services[Agentplane services]
        direction TB
        gw[Runner Gateway: queue, proxy and tunnel]
        sandboxctl[Sandbox Controller]
        policy[Policy Controller]
        hs[History Service]
        notif[Notification service]
        actions[Action Service]
        egress[Egress proxy]
        llm[LLM ingress]
        electric[Electric]
    end
    subgraph runtimes[Runner runtimes]
      direction LR
      subgraph sandbox[Sandbox Pod]
        runner[Runner + native drivers]
        sidecar[Egress sidecar]
      end
      subgraph vm_guest[VM guest]
        guest[Guest runner + native drivers]
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

    browser -->|HTTPS requests| app
    app -->|gRPC health + tunneled Runner RPCs| gw
    app -->|Dispatcher API| gw
    app --> hs
    app --> notif
    app --> actions
    app --> egress
    app --> electric
    app --> k8s

    gw --> k8s
    policy --> k8s
    sandboxctl --> k8s
    sandboxctl --> gw
    runner --> sidecar --> egress
    guest --> relay --> egress
    runner -.->|RunnerControl.Connect| sidecar
    sidecar -.->|outbound platform route| gw
    guest -.->|RunnerControl.Connect| relay
    relay -.->|outbound platform route| gw
    gw -->|tunneled Runner RPCs| runner
    gw -->|tunneled Runner RPCs| guest
    runner -.->|dials RunnerIngress.Push| sidecar
    sidecar -.->|outbound ingress route| hs
    guest -.->|dials RunnerIngress.Push| relay
    relay -.->|outbound ingress route| hs

    egress --> llm

    click app href "app/api.py" "Integration app API"
    click gw href "docs/runner_control.md" "Runner control stream and proxy"
    click hs href "proto/history_service.proto" "History Service API"
    click notif href "notification_service/api.py" "Notification Service API"
    click actions href "action_service/api.py" "Action Service API"
    click policy href "policy_controller/controller.go" "Policy Controller"
    click sandboxctl href "sandbox_controller/controller.go" "Sandbox Controller"
    click egress href "egress/addon.py" "Egress proxy"
    click llm href "llm_ingress/server.go" "LLM ingress"
    click electric href "../cluster/cdk8s/agentplane/electric.py" "Electric deployment"
    click index href "indexing/app.py" "Git index API"
```

The map shows every service and runtime placement. A VM guest runs the runner; its relay is in the
separate KubeVirt launcher Pod. Detailed API dependencies appear below. The `Git index` is a
standalone service; it polls a configured Git remote, embeds files through an embedding endpoint
and owns a separate PostgreSQL database. See the [indexing README](indexing/README.md).

### Session delivery

```mermaid
flowchart LR
    app[Integration app]
    subgraph unified[Runner Gateway]
        dispatcher[Dispatcher API and durable delivery]
        gateway[Runner proxy and control tunnel]
    end
    sandboxctl[Sandbox Controller]
    k8s[Kubernetes API]
    app -->|session opens + commands| dispatcher
    app -->|per-Sandbox Runner Proxy: live checks + explicit resume| gateway
    dispatcher -->|queued opens + commands| gateway
    sandboxctl -->|Quiesce| gateway
    dispatcher -->|watch + guarded wake| k8s
```

### Runner control tunnel

```mermaid
flowchart LR
    gateway[Runner Gateway]
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

    runner -.->|RunnerControl.Connect| sidecar
    sidecar -.->|outbound route| gateway
    guest -.->|RunnerControl.Connect| relay
    relay -.->|outbound route| gateway
    gateway -->|tunneled Runner RPCs| runner
    runner -->|responses + heartbeat| gateway
    gateway -->|tunneled Runner RPCs| guest
    guest -->|responses + heartbeat| gateway
```

The runner opens the long-lived `RunnerControl.Connect` stream through its Pod's egress sidecar or
the VM launcher's relay. The Gateway sends `Runner` RPC calls back through that existing stream,
and responses return there; neither Pod accepts an inbound Gateway connection.

### History and browser sync

```mermaid
flowchart LR
    browser([Browser])
    app[Integration app]
    history[History Service]
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

    browser -->|API + shape requests| app
    app -->|gRPC reads and watch| history
    app -->|proxied shape request| electric
    electric -->|shape response data| app
    app -->|API + SSE + shape data| browser
    runner -.->|opens RunnerIngress.Push| sidecar
    sidecar -.->|outbound platform route| history
    guest -.->|opens RunnerIngress.Push| relay
    relay -.->|outbound platform route| history
    history -->|archive and fold reads / writes| history_db
    history_db -->|logical replication data| electric
```

The app initiates each Electric HTTP request and streams the shape response through to the browser.
The separate `History PostgreSQL → Electric → app → browser` arrows show replicated and returned data.

### Notifications

```mermaid
flowchart LR
    agent([Agent])
    github([GitHub])
    notifications[Notification service]
    actions[Action Service]
    k8s[Kubernetes API]
    gateway[Runner Gateway]
    dispatcher[Runner Gateway Dispatcher API]
    history[History Service]

    agent -->|notification API| notifications
    github --> notifications
    notifications --> github
    notifications -->|Action history| actions
    notifications -->|Sandbox identity| k8s
    notifications -->|Session validation| gateway
    notifications -->|SubmitInput| dispatcher
    notifications -->|command receipts| history
```

The Notification service does not call the integration app. GitHub sends it signed webhooks; it
makes GitHub App REST calls, reads receipt evidence from History before retrying a delivery, and
submits every runner notice through the Session Dispatcher.

### Actions and egress

```mermaid
flowchart LR
    mcp_client([MCP client])
    github([GitHub / web-push])
    app[Integration app]
    actions[Action Service]
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

    mcp_client -->|MCP over OAuth| actions
    app -->|operator decisions| actions
    actions -->|create + watch Pods| k8s
    actions -->|exec via Kubernetes API| workload
    workload --> box_sidecar --> egress
    runner --> sidecar --> egress
    guest --> relay --> egress
    app -->|egress admin API| egress
    egress -->|policy and credential watches| k8s
    egress -->|MCP proxy| actions
    egress -->|policy-approved requests| other
    egress -->|model requests| llm
    llm -->|model API + server-held key| litellm
    actions -->|MCP calls| mcp_servers
    actions -->|REST + web push| github
```

The egress proxy substitutes a configured credential only for the destination covered by its
policy. LiteLLM is a separate hop behind LLM ingress; only that service holds the LiteLLM virtual key.

### Service definitions

The service nodes above link to their definitions in renderers that support Mermaid node links.
These direct links are also available in Markdown views that do not:

- gRPC: [RunnerControl](proto/runner_control.proto), [tunneled Runner RPCs](proto/runner.proto), [RunnerIngress](proto/runner_ingress.proto), [Session Dispatcher](proto/session_dispatcher.proto), and [History Service](proto/history_service.proto). The Runner Gateway also serves standard gRPC health and proxies the Runner service over each outbound control stream.
- HTTP and streaming APIs: [Integration app](app/api.py), [Electric proxy](app/electric.py), [Action Service](action_service/api.py), [Notification Service](notification_service/api.py), [LLM ingress](llm_ingress/server.go), and [Git index](indexing/app.py).
- Controllers and egress: [Sandbox Controller](sandbox_controller/controller.go), [Policy Controller](policy_controller/controller.go), [central egress proxy](egress/addon.py), [Go sidecar](egress/sidecar/main.go), and [sidecar route configuration](egress/platform_routes.py).

The integration app writes Sandbox desired state and operator grants through the Kubernetes API;
the Sandbox Controller reconciles those requests and owns `Sandbox.status`. Notifications and the
Runner Gateway read Sandbox inventory directly. The Sandbox Service Deployment, API and server
source are retired; the History Service retains its database and authored history.

- **Sessions and Threads.** A Session is a runner's raw Event stream; a Thread is its
  conversation view, folded by the History Service. Every runner pushes its Events to the History
  Service, the only way into retained history.
- **Runner traffic is outbound.** Everything a runner sends goes through its Pod's egress
  sidecar: to the egress proxy, which authenticates the Pod and substitutes credentials, or, for the
  Runner Gateway and the History Service ingress, over the sidecar's platform routes with the Pod's
  own token. Runner Pods admit no connections; a VM Sandbox's guest sends the same traffic to an
  identical relay that Kyverno adds to its KubeVirt launcher Pod. Commands and lifecycle requests reach runners over
  the `RunnerControl` stream they dial to the Runner Gateway, routed per Sandbox by its `<sandbox>-control` Service and
  `<sandbox>-runner` Lease ([runner control stream](docs/runner_control.md)).
- **Databases.** Each service owns a database on one shared CNPG cluster: `app`,
  `sandbox_service` (Session history,
  written by the History Service), `actions`, `egress`, `notifications` and `session_dispatcher`.
  The History Service retains the existing `sandbox_service` database; no database or authored
  history is removed with the Sandbox Service Deployment. The history and Dispatcher databases are drawn.
- **Sandbox lifecycle and runner control.** The app writes requested Sandbox state and operator
  grants to Kubernetes; the Sandbox Controller reconciles resources and owns status. The Runner Gateway
  implements Dispatcher Session openings and durable command admission/delivery alongside its runner
  control stream and authorized live runner access. The app reads
  current runner inventory and explicitly resumes a retained harness through the Sandbox control
  Service, with the Runner Proxy checking the live Sandbox name, UID and owner. Its queue uses the existing Dispatcher database; the Gateway has no separate registry database. The controller holds deletion until the runner has sealed its Sessions through the Gateway.

Status, open work and design gates: [`plans/`](plans/README.md).

## Implemented contracts

Use the [Action Service specification](action_service/SPEC.md) and [README](action_service/README.md) for
catalog, Decisions, events, bounded waits, cancellation, generic MCP, configured Identities, Connection grants,
and OAuth/consent. The [app README](app/README.md) owns the consent and Action-review presentation contracts.
[Executor liveness](docs/executor_liveness.md), [operator federation](docs/operator_federation.md),
[workload authentication](docs/workload_authentication.md), [launch presets](docs/launch_presets.md) and
[action policies](docs/action_policies.md) own the other implemented contracts.
[Sandbox Actions](action_service/sandbox/README.md) owns the exec-target contract for agents hosted outside the
cluster: sandboxes that run as the calling ServiceAccount, and Kubernetes reach from inside one.
[Thread, runner, and harness layering](docs/thread_layering.md) owns the command/Event architecture, Thread
identity continuity, native recovery and optional app-first acceptance. [Thread view
synchronization](docs/thread_view_sync.md) owns the conversation model and sync-engine integration, on-demand Raw,
history/catch-up and frontend state; it is distinct from the implemented raw-replay API. The rest are under
[`docs/`](docs/).
