# Agentplane

Agentplane runs coding agents in Kubernetes sandboxes and mediates what they do outside them:
network egress with credentials substituted at the proxy, operator-approved or policy-approved
Actions, and notifications. Its services are separately deployable; the Action Service alone is
an MCP approval gateway for any MCP-capable LLM product.

**Design principles:** [`docs/principles.md`](docs/principles.md). Check new designs against it.

## Services

- [Action Service](action_service/README.md): ActionRequests, Decisions, auto-approval policies,
  and an MCP endpoint over OAuth.
- [Sandbox Service](sandbox_service/README.md): Sandbox lifecycle, grants, egress and teardown.
- [Runner Gateway](runner_gateway/README.md): the runner control stream, Sessions, and command
  admission and delivery.
- [History Service](history_service/README.md): retained Session history, read by the
  integration app and the Notification service.
- [Runner](runner/README.md) and [native drivers](native/README.md): run Claude Code or Codex in a
  sandbox and journal what they do.
- [Egress proxy](egress/README.md): authenticates sandbox workloads and substitutes credentials.
- [Policy Controller](policy_controller/README.md): expires `PolicyBinding`s and keeps the
  RoleBindings they grant. Runs on staging and `agentplane-testing`; only Flux writes `PolicyBinding`s yet.
- [Sandbox Controller](sandbox_controller/README.md): reconciles our own `Sandbox` into its state
  PVC and Pods or a KubeVirt VM. Runs on `agentplane-testing` only.
- [LLM ingress](llm_ingress/README.md): authenticated hop in front of LiteLLM.
- [Notification service](notification_service/README.md): subscriptions and session inboxes.
- [Indexing](indexing/README.md): Git semantic search.
- [Integration app](app/README.md): the human-facing frontend over the services above, and a
  client of them.

## System overview

What staging runs ([`cluster/cdk8s/agentplane/`](../cluster/cdk8s/agentplane/chart.py)).

```mermaid
flowchart TB
    browser([Browser])
    mcp_client([MCP client, e.g. claude.ai])
    github([GitHub])
    app[Integration app]
    subgraph services[Backend services]
        ss[Sandbox Service]
        gw[Runner Gateway]
        hs[History Service]
        notif[Notification service]
        actions[Action Service]
        policy[Policy Controller]
        sandboxctl[Sandbox Controller]
    end
    subgraph sandbox[Sandbox Pod]
        runner[Runner + native drivers]
        sidecar[Egress sidecar]
    end
    cmd_sandbox[Action box Pods]
    proxy[Egress proxy]
    llm[LLM ingress]
    k8s[Kubernetes API, agent-sandbox controller]
    authentik([Authentik])
    electric[Electric]
    hist_db[(history DB)]
    outside((LiteLLM, internet, cluster services))

    browser -->|HTTPS, SSE| app
    mcp_client -->|MCP over OAuth| actions
    app -->|gRPC: Sandboxes| ss
    app -->|gRPC: Sessions, commands| gw
    app -->|gRPC: Session and Thread reads, change and Session-opening watch| hs
    app -->|HTTP| notif
    app -->|HTTP: operator decisions| actions
    app -->|HTTP admin port: egress decisions| proxy
    app -->|Thread shapes for the browser| electric
    app -->|SandboxTemplates, Sandbox and policy reads| k8s
    app -->|OIDC| authentik
    electric -->|logical replication| hist_db
    hist_db -.->|staging: WAL archive, on-demand base backups| outside

    ss -->|Sandbox CRs, grants; testing creates ours| k8s
    ss -->|SealSandbox| gw
    k8s -->|creates| sandbox
    gw -->|control Services, Leases; SessionsSealed on our deleting Sandboxes| k8s
    hs -->|ingest, fold, read| hist_db
    notif -->|Sandbox lookups| ss
    notif -->|commands| gw
    notif -->|command receipts| hs
    notif --> actions
    notif -->|REST| github
    github -->|webhooks over an HTTPS route| notif
    actions -->|execute: box Pods as the caller, exec| k8s
    policy -->|PolicyBindings, RoleBindings| k8s
    sandboxctl -.->|testing only: our Sandboxes, Pods, VMs, state PVCs| k8s
    k8s -->|runs| cmd_sandbox
    cmd_sandbox -->|all outbound traffic| proxy
    actions -->|MCP servers, GitHub, web push| outside

    runner --> sidecar -->|all outbound traffic| proxy
    sidecar -.->|testing, our Sandboxes: platform routes, RunnerControl Connect| gw
    sidecar -.->|testing, our Sandboxes: platform routes, RunnerIngress Push| hs
    proxy -->|policy watches, EgressReady| k8s
    proxy -->|RunnerControl Connect: commands, lifecycle| gw
    proxy -->|RunnerIngress Push: Events| hs
    proxy -->|MCP| actions
    proxy -->|HTTP| notif
    proxy -->|model calls| llm --> outside
    proxy -->|credentials substituted| outside
```

- **Sessions and Threads.** A Session is a runner's raw Event stream; a Thread is its
  conversation view, folded by the History Service. Every runner pushes its Events to the History
  Service, the only way into retained history.
- **Runner traffic is outbound.** Everything a runner sends goes through its Pod's egress
  sidecar to the egress proxy, which authenticates the Pod and substitutes credentials, and runner
  Pods admit no connections. Commands and lifecycle requests reach runners over the `RunnerControl`
  stream they dial to the Runner Gateway, routed per Sandbox by its `<sandbox>-control` Service and
  `<sandbox>-runner` Lease ([runner control stream](docs/runner_control.md)).
- **Databases.** Each service owns a database on one shared CNPG cluster: `app`,
  `sandbox_commands` (the Runner Gateway's Session registry),
  `sandbox_service` (Session history, written by the History Service), `actions`,
  `egress` and `notifications`; the Sandbox Service has none. Only the history database is drawn.
- **Sandbox Service and Runner Gateway.** The Sandbox Service owns Sandbox lifecycle, grants
  and teardown; the Runner Gateway owns everything that talks to a runner: Sessions,
  commands and the runner control stream. They run as separate Deployments with their own accounts,
  and the Sandbox Service seals a Sandbox through the gateway before deleting it.

Status, open work and design gates: [`plans/`](plans/README.md). Implemented contracts:
[`docs/`](docs/).
