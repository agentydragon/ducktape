# Agentplane system overview: focused diagrams

Per-path detail for the [README system overview](../README.md#system-overview), same conventions:
API arrows point caller to callee, labels like `response` or `replication` show data on that edge,
dashed arrows are runner-initiated platform routes.

The optional central sampler runs as its own Deployment and Service in staging and the
[k3s acceptance scenarios](../system_tests/README.md#central-sampler), using the shared cdk8s
construct, its own image and projected service-account credentials.

## Session delivery

```mermaid
flowchart LR
    app[Integration app]
    subgraph unified[Runner Gateway]
        dispatcher[Dispatcher API and durable delivery]
        gateway[Runner proxy and state observation]
    end
    central[Experimental central sampler]
    sandboxctl[Sandbox Controller]
    k8s[Kubernetes API]
    app -->|session opens + commands| dispatcher
    app -->|Dispatch watch for the browser: gRPC| dispatcher
    app -->|per-Sandbox Runner Proxy: live inventory + diagnostics| gateway
    dispatcher -->|wrapped Codex and Claude opens + commands| gateway
    dispatcher -->|central opens + commands: Sampler gRPC| central
    sandboxctl -->|Quiesce| gateway
    dispatcher -->|watch + guarded wake| k8s
```

## Central code-mode sampling (opt in)

```mermaid
flowchart LR
    dispatcher[Gateway Dispatcher]
    sampler[Central sampler and shared Codex host]
    db[(Sampler PostgreSQL)]
    gateway[Gateway ExecutorGateway]
    runner[Sandbox Runner]
    executor[Loopback Codex exec-server]
    history[History ingress and Thread fold]
    llm[LLM ingress]
    provider[OpenAI-shaped Responses API]
    dispatcher -->|Sampler gRPC: open, input, controls| sampler
    sampler -->|continuation, commands, evidence outbox| db
    sampler -->|authenticated exec-server frames| gateway
    gateway -->|mTLS RunnerExec gRPC| runner
    runner -->|WebSocket| executor
    sampler -->|acknowledged outbox: RunnerIngress.Push| history
    sampler -->|Session identity| llm --> provider
```

The sampler lazily starts one shared Codex host process when code-mode work needs it and multiplexes
logical Sessions through it. The host retains per-Session `store()` / `load()` values across cells
and idle turns; each code-mode cell runs in a fresh V8 isolate with no shared JavaScript globals.
Provider continuation stays in the sampler.
The Pod has aggregate CPU/memory limits, with no per-Session or per-cell kernel boundary today.
Gateway relays the pinned upstream exec-server protocol without interpreting tools. Dispatcher
is absent from that tool path. Sandbox UID and incarnation fence the executor binding; v1 does
not replace an executor. An unavailable executor does not remove the sampler's persisted history
or prevent its control RPCs. Existing Dispatcher routing still requires the bound Sandbox object.

History retains source evidence and derives the lossy Thread display. Its entries are not a
provider continuation format: the sampler owns that state in its database. Wrapped native
harnesses keep their current local sampling, execution and persistence paths. See the
[standalone API and recovery contract](../x/central_sampler/SPEC.md).

## Runner control

```mermaid
flowchart LR
    gateway[Runner Gateway]
    pod[Pod runner]
    guest[VM guest runner]
    gateway -->|mTLS Runner + RunnerExec| pod
    gateway -->|mTLS Runner + RunnerExec| guest
```

The Gateway dials native gRPC and a state watch on each runner. Incarnation fencing, connection
Leases and command IDs protect runner-owned journals. History push remains a separate outbound platform route.

## Codex execution inside a Sandbox

```mermaid
flowchart LR
    subgraph pod[Sandbox Pod]
        runner[Runner and Codex app-server]
        exec[Executor companion and exec-server]
        runner -->|Unix ownership connection| exec
        runner -->|loopback WebSocket tools| exec
    end
    subgraph vm[VM guest]
        guest[Runner + COSI controllers]
        manager[systemd manager]
        harness[Codex app-server service: workload slice]
        vmexec[exec-server service: executor slice]
        guest -->|D-Bus launch, observe, stop, fence| manager
        manager -->|service lifetime and cgroup| vmexec
        manager -->|service lifetime and cgroup| harness
        harness -->|loopback WebSocket tools| vmexec
    end
```

The Pod executor has a private PID namespace and Kubernetes resource limits. The VM executor has a
sibling systemd slice budget. Both share the Sandbox's identity and filesystem paths with the harness.
Runner owns restart and fences old commands before reopening Session state or sealing journals.
Local execution remains a deployment mode. See [executor isolation](../runner/docs/codex_executor.md).

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
    runner -.->|OTLP/gRPC 127.0.0.1:3132| sidecar
    sidecar -->|OTLP/gRPC :4317| alloy
    guest -.->|OTLP/gRPC 10.0.2.1:3132| relay
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
    browser -->|API + shape requests, same origin| app
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
The HTTPS Gateway routes static files directly to the frontend Service and all API, login and stream
requests to the app Service. The frontend has no backend credentials or outbound service calls.

## Notifications

```mermaid
flowchart LR
    agent([Agent])
    app[Integration app]
    github([GitHub])
    notifications[Notification service]
    actions[Action Service]
    k8s[Kubernetes API]
    gateway[Runner Gateway]
    dispatcher[Runner Gateway Dispatcher API]
    history[History Service]

    agent -->|subscriptions, inboxes, messages| notifications
    app -->|gRPC operator watches + health| notifications
    github --> notifications
    notifications --> github
    notifications -->|Action history: gRPC| actions
    notifications -->|Sandbox identity, watched send grants| k8s
    notifications -->|Session validation| gateway
    notifications -->|SubmitInput| dispatcher
    notifications -->|command receipts, Session's Sandbox| history
```

The Notification service does not call the integration app, which watches subscription and notice
state over the operator-only `NotificationsOperator` gRPC service. GitHub sends it signed webhooks; it
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
    edge[MCP edge]
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
    app -->|Action RPCs: gRPC; MCP OAuth callback: HTTP| actions
    app -->|ConnectionService RPCs and health: gRPC| edge
    edge -->|account scoped gRPC| actions
    actions -->|create + watch Pods| k8s
    actions -->|exec via Kubernetes API| workload
    workload --> box_sidecar --> egress
    runner --> sidecar --> egress
    guest --> relay --> egress
    app -->|egress admin API| egress
    egress -->|policy and credential watches| k8s
    egress -->|workload Actions gRPC and MCP| actions
    egress -->|policy-approved requests| other
    egress -->|model requests| llm
    llm -->|model API + server-held key| litellm
    actions -->|MCP calls| mcp_servers
    actions -->|REST + web push| github
```

The egress proxy substitutes a configured credential only for the destination covered by its
policy. LiteLLM is a separate hop behind LLM ingress; only that service holds the LiteLLM virtual key.
The app has independent `action_federation.service_url` and `connection_service_url` settings.
Both tiers send operator Connection calls and gRPC health probes to the [MCP edge](../mcp_edge/README.md).
Staging's external MCP/OAuth routes terminate there too, preserving the public issuer/resource URLs.
Actions retains upstream MCP linkage and its public client metadata; it no longer serves staging's
integrated ConnectionService or OAuth issuer. The edge verifies operator OIDC separately from
external MCP bearers and forwards workload calls under the bound ServiceAccount's token.

## Service definitions

- gRPC: [Actions](../action_service/action_service.proto), [operator Actions](../action_service/operator_action_service.proto), [Connections](../mcp_edge/connection_service.proto), [Web Push](../action_service/web_push_service.proto), [Runner RPCs](../runner/runner_service.proto), [RunnerIngress](../history_service/runner_ingress.proto), [Session Dispatcher](../runner_gateway/dispatcher/session_dispatcher.proto), [History Service](../history_service/history_service.proto), and the Notification Service's [workload](../notification_service/notifications.proto) and [operator](../notification_service/notifications_operator.proto) APIs (the workload API also has an HTTP form). The Runner Gateway also serves standard gRPC health and proxies the Runner service over a direct mTLS connection to each runner.
- HTTP and streaming APIs: [Integration app](../app/api.py), [Electric proxy](../app/electric.py), [Action Service generated facade](../action_service/action_service.proto), [LLM ingress](../llm_ingress/server.go), and [Git index](../indexing/app.py).
- Controllers and egress: [Sandbox Controller](../sandbox_controller/controller.go), [Policy Controller](../policy_controller/controller.go), [central egress proxy](../egress/proxy/proxy.go), [Go sidecar](../egress/sidecar/main.go), and [sidecar route configuration](../egress/sidecar/configpb/config.proto).
