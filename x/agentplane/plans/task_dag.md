# Agentplane task DAG

This is the single project overview for Agentplane work. It is a dependency map, not a workflow
engine or a request to build every future subsystem. Boxes are sized as coherent agent work packages
that ship as their own PRs; the detailed tasks live in the supporting documents. An edge is a real
dependency, meaning the downstream package cannot be specified or tested without the upstream one.
Packages without an edge between them are meant to be worked in parallel, and a conflict between
two of them is resolved by whoever lands second rebasing.

## Outcome

Rai can open a separate integration app backed by Agentplane, create a sandbox running one Claude
or Codex runner, send an Input, watch response and tool activity arrive, detach and come back to
what happened since, suspend and resume the sandbox with the conversation intact, and read the raw
native frames behind any event. The first instance is a staging one on the cheap-experiments
model key, and the agent working on Agentplane can drive it end to end without Rai: create a
sandbox, run a turn, read what happened, tear it down. The first functioning product is
credentialless toward external systems; real upstream credentials are a later gate. Sandboxes are disposable, trajectories are
not: what an agent did, and why, outlives the sandbox it ran in, under a name, and is searchable
later. Beyond this slice, the experiences the stretch nodes exist for are the three stories in
[`user_stories.md`](user_stories.md): asks Rai decides, a trusted orchestrator delegating
public-only work to an untrusted fleet under a judge, an orchestrator running specialists, Haku itself as a long-lived agent here, and a UI Haku
authors and is driven through.

## Current status

- **Landed:** the sandbox proxy/identity spike's evidence; the native drivers and scripted harness
  tests ([`../native/`](../native/), [`../harness_tests/`](../harness_tests/),
  [`../capture/`](../capture/)); the runner protocol and service
  ([`../runner/`](../runner/), [`../runner/SPEC.md`](../runner/SPEC.md)) with a durable
  per-session log, cursor reattach, idempotent inputs, and restart recovery, pinned by one set of
  interaction scripts run against both binaries; typed wire models for both harness vocabularies
  and the `py_grpc_library` macro that generates the protocol stubs; the runner image with both
  harnesses, the runner's Pod contract (`ListSessions`, the SIGTERM ladder), the
  `agentplane-staging` namespace with its `SandboxTemplate` on the cheap-experiments key and the
  agent's standing access; and the first integration app ([`../app/`](../app/)): the sandbox
  inventory with archive, the runner bridge with SSE fanned out to every tab, the UI with its
  phone layout, the staging deployment behind Authentik, and the PostgreSQL trajectory store.
  The first functioning Agentplane (`F0`) is reached: both providers ran real turns on staging
  from the app, and a sandbox came back from suspension with its conversation.
- **Next:** the secure egress integration (`J`), the gate for every credentialed external system
  and for stories 1 and 2. Named threads (`T2`) and search (`T3`) wait only on a first user of
  the persisted history.
- **Access-control scope is intentionally deferred:** the current Ducktape work can use its existing
  broad internet boundary and scoped GitHub credential for `agentydragon-agent`; that convenience is
  not a policy model for the private, high-context Haku agent.

## DAG

```mermaid
flowchart TB
    classDef completed fill:#dcfce7,stroke:#15803d,color:#14532d,stroke-width:2px
    classDef ready fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a,stroke-width:3px
    classDef next fill:#e0f2fe,stroke:#0369a1,color:#0c4a6e,stroke-width:2px
    classDef inflight fill:#fef9c3,stroke:#a16207,color:#713f12,stroke-width:2px
    classDef milestone fill:#ede9fe,stroke:#6d28d9,color:#4c1d95,stroke-width:2px
    classDef decision fill:#ffedd5,stroke:#c2410c,color:#7c2d12,stroke-width:2px,stroke-dasharray:5 3
    classDef future fill:#f3f4f6,stroke:#6b7280,color:#374151

    S0["Sandbox proxy/identity<br/>completed evidence"]:::completed
    A["Native drivers + scripted harness tests"]:::completed
    B["Runner protocol + both adapters<br/>durable log, reattach, restart recovery"]:::completed

    subgraph pod["Runner in a Sandbox"]
        I1["I1 Runner image<br/>both harnesses, Docker smoke test"]:::completed
        I2["I2 Runner pod contract<br/>Pod listen, env config, ListSessions, SIGTERM ladder"]:::completed
        I3["I3 Staging namespace<br/>sandbox template, PVC, cheap-experiments key,<br/>standing agent access"]:::completed
        I4["I4 First real turn + suspend/resume continuity<br/>manual milestone"]:::completed
    end

    subgraph app["Integration app v0"]
        C1["C1 Sandbox inventory<br/>list, create, suspend, resume, delete"]:::completed
        C2["C2 Runner bridge<br/>REST + SSE over Attach, fan-out to every tab"]:::completed
        C3["C3 UI<br/>sandboxes, session stream, raw view, input, interrupt"]:::completed
        C4["C4 App deployment into staging<br/>RBAC, Authentik route, agent-reachable API"]:::completed
        C5["C5 Archive<br/>out of the active view, history kept"]:::completed
        C6["C6 Session view legibility<br/>markdown, the input's own text, folded reasoning,<br/>Enter sends with no button, raw frames in place"]:::ready
        C7["C7 Lifecycle controls on the sandbox page<br/>suspend there, delete once suspended"]:::ready
        C8["C8 Live push for every view<br/>the server says what changed, no page polls"]:::ready
        C9["C9 The profile a sandbox runs under<br/>visible and pickable, not one badge"]:::ready
        C10["C10 Egress actions that say what they do<br/>approve / deny / revoke, and Flux ownership"]:::ready
    end

    F0["First functioning Agentplane<br/>both providers in sandboxes, driven and replayed from the app"]:::completed

    subgraph traj["Trajectories outlive sandboxes"]
        T1["T1 Trajectory persistence<br/>session log + native frames copied out of the sandbox<br/>keyed by thread, sandbox, agent"]:::completed
        T2["T2 Named threads<br/>a small model proposes, the user edits"]:::next
        T3["T3 Search and lookup over past interactions<br/>what happened, why, which agent"]:::next
    end
    D["Decided<br/>conversation app: a separate deployment"]:::completed
    E["Conversation app<br/>timeline and live control over persisted threads"]:::future
    F["Product milestone<br/>persisted history, honest outcomes, real users"]:::milestone

    G["Rai decision<br/>second viewer on one session needed?"]:::decision
    R1["Read-only follower attachments"]:::future

    J["Secure egress integration<br/>per-Pod sidecar wraps traffic with the Pod's SA token;<br/>central proxy holds credentials and egress policy;<br/>first credential: the agentydragon-agent GitHub PAT"]:::ready
    P["Rai decision<br/>dynamic per-Thread policy or explicit approval needed?"]:::decision
    K["Conditional access controller<br/>allow / deny / user approval required"]:::future
    R["Rai decision<br/>does the threat model require stronger isolation?"]:::decision
    L["Credentialed production readiness<br/>runner port auth, freshness, replay, rotation"]:::future
    V["Stronger runtime evaluation<br/>gVisor, Kata, Firecracker, or equivalent"]:::future

    Q["Rai decision<br/>which observed reliability failure is next highest-cost?"]:::decision
    M["Reliability hardening only from observed failures<br/>mid-tool crash recovery, log compaction, harness pin refresh"]:::future

    N["Story 2: trusted orchestrator, untrusted fleet<br/>tiers, events kind filter, channel judge<br/>user_stories.md"]:::future
    O["Story 3: an orchestrator with specialists<br/>fleet view, wake-ups, tier-scoped reads"]:::future
    HK["Story 4: Haku lives here<br/>long-lived thread, memory in git"]:::future
    UI["Story 5: agentic UI<br/>Haku-authored pages, events as inputs"]:::future
    X["Decided<br/>approval decisions and notifications arrive as thread inputs"]:::completed
    Y["Story 1: ask me, I decide<br/>asks, delivery, decisions as inputs<br/>async_approvals.md, user_stories.md"]:::future
    Z["Rai decision<br/>what explicit Haku Console integration is needed?"]:::decision
    U["Stretch<br/>Haku Console link, adapter, or enveloped message path"]:::future
    AA["Rai decision<br/>what permission and policy model is acceptable for private Haku?"]:::decision
    AB["Stretch<br/>Agent Console access-control track<br/>external_access.md"]:::future
    AC["Stretch<br/>Haku-ready policy enforcement<br/>private context, least privilege, resilient controls"]:::future
    W["Stretch<br/>hardened Kubernetes/Authentik deployment"]:::future

    A --> B
    B --> I1
    B --> I2
    I1 --> I4
    I2 --> I4
    I3 --> I4
    B --> C2
    I2 --> C2
    C1 --> C3
    C2 --> C3
    C1 --> C4
    C2 --> C4
    C1 --> C5
    C3 --> C6
    C1 --> C7
    C3 --> C8
    C1 --> C9
    C4 --> C10
    C5 --> E
    I4 --> F0
    C3 --> F0
    C4 --> F0
    C2 --> T1
    T1 --> T2
    T1 --> T3
    F0 --> D
    D --> E
    T1 --> E
    T2 --> E
    E --> F
    T3 --> F
    F0 --> G -->|yes| R1
    F0 --> J --> P
    P -->|yes| K --> R
    P -->|no| R
    R -->|yes| V --> L
    R -->|no| L
    F0 --> Q --> M
    J --> N
    T1 --> N
    Y --> N
    N --> O
    I4 --> HK
    T1 --> HK
    HK --> UI
    Y --> UI
    T3 --> O
    F0 --> X --> Y
    J --> Y
    E --> Z --> U
    F --> AA --> AB --> AC
    L --> W
    S0 -. informs .-> J
    S0 -. suspend/resume evidence .-> I3
```

Legend: green is landed; yellow is in flight, with a PR open; the bold blue nodes are ready to
start now, in parallel; light blue is next work blocked only on a node in this slice; purple is a
milestone;
orange diamonds are unresolved decisions requiring Rai's product or design input; gray is
conditional or stretch work.

Ready now: **J** and **C6**-**C10**, which share nothing and can run in parallel. **T2** and **T3** are
blocked on nothing in code; they start when the persisted history has a first reader who needs a
name or a search.

The orange nodes are deliberately limited to choices that change downstream implementation
ordering. `D` is decided: the conversation app stays a separate deployment, at least for now.
T1's store is PostgreSQL (a CNPG cluster beside the runner Pods; staging's is disposable).
External-event scope is decided: approval decisions and other notifications reach a thread as
inputs ([`async_approvals.md`](async_approvals.md)). A "no" choice should close or defer that
branch rather than create speculative scaffolding.

`J` is decided in shape: the ADR's composition, a per-Pod sidecar that takes unauthenticated
traffic from the sandbox and forwards it to a central proxy under the Pod-bound ServiceAccount
token, with the central proxy holding the real credentials and the per-identity egress rules
(methods, hosts, paths, and which placeholder tokens it substitutes). It is Agentplane's own
code, not a reuse of Haku's egress proxy, and the integration app shows a sandbox's allowed
egress, tokens, and decisions. The `P`/`K` path is only the narrow access decision needed for a
credentialed Agentplane egress deployment. It is not the general Agent Console permission model represented by `AA`/`AB`, and it must
not be reused as a private-Haku policy language by default. The existing broad Ducktape lane is a
scoped operational convenience, not evidence that the same grants are safe for an agent with Rai's
personal context.

## Work-package acceptance

Landed packages are described where they live (§ Current status); what follows is the bar for the
ones still open.

- **C6 session view legibility:** five things that make a real transcript hard to read today, and
  they are not all UI work.
  - **Markdown for assistant text**, which currently renders as literal `**` and backticks.
  - **Reasoning folded** into a disclosure widget, closed by default, so the answer is not buried
    under the thinking.
  - **Enter sends, Ctrl+Enter takes a newline**, and the Send button goes away entirely: the
    textarea's placeholder carries the binding, so the composer is one control instead of two.
    Interrupt stays, as an icon rather than a text button — the same move the sandbox list already
    made for Suspend and Resume, so `@tabler/icons-react` via per-icon subpath imports (the barrel
    OOMs esbuild on RBE).
  - **A user input shows what was typed.** Today it shows only its opaque `input_id`, because
    `InputSubmitted` carries the id alone ([`../runner/protocol.proto`](../runner/protocol.proto)).
    Decided (Rai): the protocol changes, so the text arrives with the event and a client that
    joined later or replayed from the log sees it too — which is the case an app-side echo of what
    this tab sent cannot cover.
  - **Raw frames interleave rather than replace.** The switch currently swaps the whole transcript
    for a flat list of frames, which loses the place you were reading. Instead each frame appears
    beside the neutral item it produced and disappears again when the switch goes off. That needs
    the projection to keep the link it discards today — which event sequences fed which item — so
    it is a change in `events.ts`, not only in the markup.
- **C7 lifecycle controls on the sandbox page:** the page you are already looking at can suspend
  the sandbox, and delete it once it is suspended. Today every lifecycle action lives on the list
  page only (`sandboxes.tsx`), so acting on the sandbox in front of you means navigating away from
  it. Deleting only a suspended sandbox is a new rule either way: `Inventory.delete` currently
  takes any state, so the package decides whether the precondition belongs in the API — where it
  also binds the agent driving staging — or is a UI affordance over an API that stays permissive.
- **C8 live push for every view:** the server tells the browser what changed; no page polls.
  Decided (Rai): a push channel, not a shorter interval. Three surfaces poll at five seconds today
  — the sandbox list, the sandbox page, and the egress tab — each hand-rolling the same
  `useEffect`/`setInterval` pair, and a fourth was written with no refresh at all (`listThreads`
  runs once on mount, so a session named after you arrived keeps its old name until a reload).
  Polling is also why a state change takes up to five seconds to appear and why every view costs a
  request per client per interval whether or not anything happened.
  The app already runs one push channel — the session's SSE stream — so the shape exists; what
  does not exist is a source to push _from_. `inventory.py` and `egress.py` answer each request
  with a fresh list against the API server and hold nothing between requests, so this package's
  real content is server-side: a watch the app keeps over Sandboxes and EgressBindings, and a
  stream that fans its changes out to the open tabs. The egress proxy's `Informer`
  ([`../egress/informer.py`](../egress/informer.py)) is the shape to reuse rather than reinvent —
  same list-and-watch, same freshness question, and its `/healthz` already answers "has this
  stopped moving", which a UI feeding off a watch will need too.
- **C9 the profile a sandbox runs under:** a profile decides what the sandbox may reach — a
  Flux-managed `EgressBinding` selects on the label it stamps — and it is nearly invisible. It
  appears as one badge on the sandbox page, and at creation as a free-text box whose help text
  ("a label the profile bindings select on") assumes you already know which profiles exist. The
  list page does not show it although the API already returns it on every row, nothing can be
  filtered by it, and a typo silently yields a sandbox that matches no binding. Make it a pick
  from the profiles that actually exist, show it wherever a sandbox is shown, and say what the one
  you picked grants.
- **C10 egress actions that say what they do:** the binding row offers Approve, Deny, and Revoke,
  and nothing on screen distinguishes the last two — both read as "stop this access". They are not
  the same: Deny sets `approval.state` and keeps the binding, so it is reversible and leaves the
  record of who decided; Revoke deletes the object. Worse, Deny does not know what Revoke knows.
  Revoke is disabled for a Flux-applied binding with a tooltip saying to remove it in git, but Deny
  stays live — and `egressbinding-sandboxes-github-public.yaml` carries `approval.state: approved`
  in git, so denying it patches the cluster and the next reconcile puts it back. A button that
  silently un-does itself is the bug; the wording is the rest of the package.
- **T2 named threads:** a small model proposes a name from the first turn, the user can edit it,
  and the name lives on the thread record; naming never touches the runner or the harness.
- **T3 search and lookup:** find past interactions by text and by what an agent did; answer "what
  happened here", "why did the agent do that", and "which agent did this" from the persisted
  trajectory, with the raw frames one step away.
- **Conversation app (E):** timeline and live control over persisted threads, as a client of the
  same API; how archived threads are presented stays in this layer. The archived flag still lives
  on the Sandbox; it moves to the thread record here, after which an archived sandbox may be
  deleted without losing anything.
- **Secure egress and credentialed readiness:** one narrow synthetic operation proves the fixed sidecar
  to trusted gateway path before any real upstream credential is enabled. Real credentials remain only
  at the gateway; durable freshness/replay, per-Sandbox/Thread binding, rotation, runner-port
  authentication, and escape tests gate production use.
- **Reliability:** choose one observed failure with the highest user cost after the first functioning
  product. Candidates already known: recovery of a turn lost mid-tool beyond `PROCESS_LOST`, session
  log growth, and the harness pin refresh workflow. Each hardening slice needs its own reproduction
  and acceptance test; do not implement every candidate in advance.
- **Stretch branches:** collaboration, external events, Haku Console integration, stronger runtimes,
  hardened deployment, and the private-Haku access-control track each begin only after the
  corresponding decision node and dependencies are resolved. The private-Haku track must cover tool
  execution, HTTP/API calls, credential handling, approvals, and policy behavior without assuming that
  routine per-action approval clicks are a reliable safety mechanism.

## Ownership and sequencing rules

- Kubernetes/Agent Sandbox owns Sandbox, Pod, PVC, readiness, suspension, and workload lifecycle.
- Native harnesses own native history, execution semantics, and provider-native resume.
- The runner owns harness supervision, the session log, and the protocol; Agentplane's app owns
  the sandbox inventory it derives from Kubernetes, the browser API, and later the live
  `Pod -> Sandbox -> Thread -> Agent` mapping once that mapping is required.
- The conversation app owns product UX such as generated Thread names, naming persistence, archive
  presentation, and timeline behavior. It may remain separate or later be hosted by Haku Console, but
  must not create shared runtime, route, frontend, or persistence coupling.
- Do not add a persistence schema, UI projection, Kubernetes controller, credential path, or
  approval framework ahead of the first test or feature that cannot pass without it. Trajectory
  persistence (T1) is that feature for the database: it enters with T1, not before.
- Do not enable real upstream credentials until the secure-egress and credentialed-readiness gates pass.
- One runner per sandbox and one runner attachment per session are acceptable for the first
  functioning product; the app fans that attachment out to every browser tab on the session.
  Multi-Agent rooms, subscriptions, external events, advanced retention, and provider migration
  are not hidden prerequisites.

## Detailed plans

- The integration app's shape and decisions: [app README](../app/README.md).
- The secure egress integration, its resources and packages: [`egress_proxy.md`](egress_proxy.md).
- Native provider scenarios and the scripted-test workflow: [`experiments.md`](experiments.md), the
  [native driver README](../native/README.md), the [harness tests README](../harness_tests/README.md),
  and the [live capture probe README](../capture/README.md).
- The runner protocol and its tests: [runner README](../runner/README.md) and
  [runner SPEC](../runner/SPEC.md).
- Sandbox identity and egress evidence: [sandbox spike README](../sandbox-spike/README.md) and [egress
  ADR](adr_sandbox_proxy_gateway.md).
- Product/API layering and deferred capabilities: [`product_surface.md`](product_surface.md) and
  [`architecture.md`](architecture.md).
- Asynchronous approvals, decision delivery, and the notification batcher:
  [`async_approvals.md`](async_approvals.md).
- Delegated identity versus brokered credentials per external system, and the rule for choosing:
  [`external_access.md`](external_access.md).
