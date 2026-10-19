# Agentplane system overview: focused diagrams

Per-path detail for the [README system overview](../README.md#system-overview), same conventions:
API arrows point caller to callee, labels like `response` or `replication` show data on that edge,
dashed arrows are runner-initiated platform routes.

## Session delivery

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
    app -->|Dispatch watch for the browser, over HTTP/JSON| dispatcher
    app -->|per-Sandbox Runner Proxy: live inventory + diagnostics| gateway
    dispatcher -->|queued opens + commands| gateway
    sandboxctl -->|Quiesce| gateway
    dispatcher -->|watch + guarded wake| k8s
```

## Runner control tunnel

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

## History and browser sync

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

## Notifications

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

## Actions and egress

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

## Service definitions

- gRPC: [RunnerControl](../proto/runner_control.proto), [tunneled Runner RPCs](../proto/runner.proto), [RunnerIngress](../proto/runner_ingress.proto), [Session Dispatcher](../proto/session_dispatcher.proto), and [History Service](../proto/history_service.proto). The Runner Gateway also serves standard gRPC health and proxies the Runner service over each outbound control stream.
- HTTP and streaming APIs: [Integration app](../app/api.py), [Electric proxy](../app/electric.py), [Action Service](../action_service/api.py), [Notification Service](../notification_service/api.py), [LLM ingress](../llm_ingress/server.go), and [Git index](../indexing/app.py).
- Controllers and egress: [Sandbox Controller](../sandbox_controller/controller.go), [Policy Controller](../policy_controller/controller.go), [central egress proxy](../egress/proxy/proxy.go), [Go sidecar](../egress/sidecar/main.go), and [sidecar route configuration](../egress/platform_routes.py).
