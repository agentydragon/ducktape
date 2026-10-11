# Agentplane

Agentplane runs coding agents in Kubernetes sandboxes and mediates what they do outside them:
network egress with credentials substituted at the proxy, operator-approved or policy-approved
Actions, and notifications. Its services are separately deployable; the Action Service alone is
an MCP approval gateway for any MCP-capable LLM product.

**Design principles:** [`docs/principles.md`](docs/principles.md). Check new designs against it.

## Services

- [Action Service](action_service/README.md): ActionRequests, Decisions, auto-approval policies,
  and an MCP endpoint over OAuth.
- [Sandbox Service](sandbox_service/README.md): sandbox lifecycle and access to runner sessions.
- [History Service](history_service/README.md): retained Session history, read by the
  integration app.
- [Runner](runner/README.md) and [native drivers](native/README.md): run Claude Code or Codex in a
  sandbox and journal what they do.
- [Egress proxy](egress/README.md): authenticates sandbox workloads and substitutes credentials.
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
    app[Integration app]
    subgraph services[Backend services]
        ss[Sandbox Service]
        gw[Runner Gateway]
        hs[History Service]
        hi[History ingress]
        notif[Notification service]
        actions[Action Service]
    end
    subgraph sandbox[Sandbox Pod]
        runner[Runner + native drivers]
        sidecar[Egress sidecar]
    end
    proxy[Egress proxy]
    llm[LLM ingress]
    k8s[Kubernetes API, agent-sandbox controller]
    electric[Electric]
    hist_db[(history DB)]
    outside((LiteLLM, internet, cluster services))

    browser -->|HTTPS, SSE| app
    mcp_client -->|MCP over OAuth| actions
    app -->|gRPC: Sandboxes| ss
    app -->|gRPC: Sessions, commands| gw
    app -->|gRPC: Session and Thread reads| hs
    app -->|HTTP| notif
    app -->|HTTP: operator decisions| actions
    app -->|Thread shapes for the browser| electric
    electric -->|logical replication| hist_db

    ss -->|Sandbox CRs, grants| k8s
    ss -->|SealSandbox| gw
    k8s -->|creates| sandbox
    gw -->|control Services, Leases| k8s
    gw -->|gRPC Attach: event follow| runner
    hs -->|WatchSessions, FollowSession| gw
    hs -->|retention holds| ss
    hs -->|ingest, fold, read| hist_db
    hi -->|append| hist_db
    notif -->|Sandbox lookups| ss
    notif -->|commands| gw
    notif -->|command receipts| hs
    notif --> actions
    actions -->|execute| k8s

    runner --> sidecar -->|all outbound traffic| proxy
    proxy -->|RunnerControl Connect: commands, lifecycle| gw
    proxy -->|RunnerIngress Push: Events| hi
    proxy -->|MCP| actions
    proxy -->|HTTP| notif
    proxy -->|model calls| llm --> outside
    proxy -->|credentials substituted| outside
```

- **Sessions and Threads.** A Session is a runner's raw Event stream; a Thread is its
  conversation view, folded by the History Service. Every new Sandbox's runner pushes its Events to
  the History ingress. The History Service also follows each Session through the Runner Gateway,
  which reads it from the runner over `Attach`; for a Sandbox created before runners pushed, that
  is the only path, and both write the same log.
- **Runner traffic is outbound.** Everything a runner sends goes through its Pod's egress
  sidecar to the egress proxy, which authenticates the Pod and substitutes credentials. Commands
  reach staging's runners over the `RunnerControl` stream they dial to the Runner Gateway, routed
  per Sandbox by its `<sandbox>-control` Service and `<sandbox>-runner` Lease
  ([runner control stream](docs/runner_control.md)).
- **Databases.** Each service owns a database on one shared CNPG cluster: `app`,
  `sandbox_commands` (Runner Gateway command admission), `sandbox_service` (Session history,
  written by the History Service and its ingress), `actions`, `egress` and `notifications`. Only
  the history database is drawn. A second Electric over the `app` database still runs, but
  staging's app reads Threads through the history one.
- **Sandbox Service and Runner Gateway.** The Sandbox Service owns Sandbox lifecycle, grants,
  retention holds and teardown; the Runner Gateway owns everything that talks to a runner: Sessions,
  commands and the runner control stream. They run as separate Deployments with their own accounts,
  and the Sandbox Service seals a Sandbox through the gateway before deleting it.

Status, open work and design gates: [`plans/`](plans/README.md). Implemented contracts:
[`docs/`](docs/).
