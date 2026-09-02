# Agentplane task DAG

This is the single project overview for Agentplane work. It is a dependency map, not a workflow
engine or a request to build every future subsystem. Boxes are sized as coherent agent work packages;
the detailed provider/scenario tasks remain in the supporting experiment documents.

## Outcome

Rai can use a separate conversation-style app backed by a standalone Agentplane service to create a
Thread, start one Claude or Codex runner, send an Input, watch native/model-backed response and tool
activity arrive, and see an honest terminal outcome. The first functioning product is credentialless;
real upstream credentials are a later gate.

## Current status

- **Sandbox proxy/identity evidence is complete:** the standalone spike proved proxy-only Secret
  delivery, Pod/Sandbox workload authentication, replay rejection, and the same-Pod route-confinement
  limitation. It informs later egress work but does not gate the native path.
- **Native drivers and scripted harness tests are in place:** [`../native/`](../native/) drives
  both pinned binaries over stdio, [`../harness_tests/`](../harness_tests/) pins their behavior
  against a loopback model each test scripts one request at a time
  (`bbr test //x/agentplane/harness_tests/...`), and [`../capture/`](../capture/) is the live probe
  whose logs are read when a script is authored or repaired. No recordings are committed.
- **Next:** give one agent end-to-end ownership of the shared stdio protocol and both provider adapters.
- **Access-control scope is intentionally deferred:** the current Ducktape work can use its existing
  broad internet boundary and scoped GitHub credential for `agentydragon-agent`; that convenience is
  not a policy model for the private, high-context Haku agent.

## DAG

```mermaid
flowchart TB
    classDef completed fill:#dcfce7,stroke:#15803d,color:#14532d,stroke-width:2px
    classDef next fill:#dbeafe,stroke:#1d4ed8,color:#1e3a8a,stroke-width:2px
    classDef milestone fill:#ede9fe,stroke:#6d28d9,color:#4c1d95,stroke-width:2px
    classDef decision fill:#ffedd5,stroke:#c2410c,color:#7c2d12,stroke-width:2px,stroke-dasharray:5 3
    classDef future fill:#f3f4f6,stroke:#6b7280,color:#374151

    S0["Sandbox proxy/identity<br/>completed evidence"]:::completed
    A["Native drivers + scripted harness tests<br/>landed"]:::completed
    B["Shared stdio protocol<br/>+ both provider adapters"]:::next
    C["Standalone Agentplane service seam<br/>records, runner bridge, REST/SSE"]:::next
    D["Rai decision<br/>initial conversation-app hosting boundary"]:::decision
    E["Conversation app/UI<br/>Thread naming, archive, timeline, live control"]:::next
    F["First functioning credentialless Agentplane<br/>both providers, persisted history, honest outcomes"]:::milestone

    J["Secure egress integration<br/>fixed sidecar + trusted external gateway"]:::next
    P["Rai decision<br/>dynamic per-Thread policy or explicit approval needed?"]:::decision
    K["Conditional access controller<br/>allow / deny / user approval required"]:::future
    R["Rai decision<br/>does the threat model require stronger isolation?"]:::decision
    L["Credentialed production readiness<br/>freshness, replay, rotation, failure semantics"]:::future
    V["Stronger runtime evaluation<br/>gVisor, Kata, Firecracker, or equivalent"]:::future

    Q["Rai decision<br/>which observed reliability failure is next highest-cost?"]:::decision
    M["Reliability hardening<br/>only from observed failures"]:::future

    H["Rai decision<br/>is multi-Agent collaboration or Room semantics needed?"]:::decision
    N["Stretch<br/>multi-Agent collaboration / Room projection"]:::future
    X["Rai decision<br/>should subscriptions or external events be first-class inputs?"]:::decision
    Y["Stretch<br/>delivery envelopes, subscriptions, external events"]:::future
    Z["Rai decision<br/>what explicit Haku Console integration is needed?"]:::decision
    U["Stretch<br/>Haku Console link, adapter, or enveloped message path"]:::future
    AA["Rai decision<br/>what permission and policy model is acceptable for private Haku?"]:::decision
    AB["Stretch<br/>Agent Console access-control track<br/>tool/API permissions, credentials, approvals"]:::future
    AC["Stretch<br/>Haku-ready policy enforcement<br/>private context, least privilege, resilient controls"]:::future
    W["Stretch<br/>hardened Kubernetes/Authentik deployment"]:::future

    A --> B --> C --> D --> E --> F
    F --> J --> P
    P -->|yes| K --> R
    P -->|no| R
    R -->|yes| V --> L
    R -->|no| L
    F --> Q --> M
    F --> H --> N
    F --> X --> Y
    E --> Z --> U
    F --> AA --> AB --> AC
    L --> W
    S0 -. informs .-> J
```

Legend: green is landed work (the sandbox spike's evidence and the native drivers with their
scripted tests); blue is the next focused work; purple is the first
functioning-product milestone; orange diamonds are unresolved decisions requiring Rai's product or
design input; gray is conditional or stretch work.

The orange nodes are deliberately limited to choices that change downstream implementation ordering:
initial app hosting, dynamic policy/approval, stronger isolation, reliability priority, collaboration
semantics, external-event scope, Haku Console integration mode, and private-Haku permission policy. A
“no” choice should close or defer that branch rather than create speculative scaffolding.

The `P`/`K` path is only the narrow access decision needed for a credentialed Agentplane egress
deployment. It is not the general Agent Console permission model represented by `AA`/`AB`, and it must
not be reused as a private-Haku policy language by default. The existing broad Ducktape lane is a
scoped operational convenience, not evidence that the same grants are safe for an agent with Rai's
personal context.

## Work-package acceptance

- **Sandbox evidence:** the committed spike and evidence memo distinguish proven credential exclusion,
  Pod/Sandbox authentication, replay/lifecycle behavior, and unsupported same-Pod route confinement.
- **Native drivers + scripted harness tests:** real Claude and Codex binaries driven over stdio by
  [`../native/`](../native/); the scripted tests in [`../harness_tests/`](../harness_tests/) cover
  turns, idle resume, tool round trips, steering/interrupt and mid-turn input, and upstream
  connection loss for both providers, asserting each step's upstream request markers and native
  frames. The scenario matrix is in [`experiments.md`](experiments.md); what each harness exposes
  beyond the tests is in [`../native/docs/protocol_roster.md`](../native/docs/protocol_roster.md).
- **Live capture probe:** [`../capture/`](../capture/) records raw native and upstream logs outside
  Git as reference material for authoring or repairing a script, never as test inputs. A harness
  bump or newly tested protocol area requires a probe run, human inspection of the differences
  against the scripted expectations, and a script update.
- **Shared protocol + adapters:** one stdio contract justified by captured native frames, with both
  Claude and Codex adapters exercised through the same shared seam. This is one agent-owned package,
  not separate provider projects.
- **Standalone service seam:** Agentplane owns its service, API, persistence, runner bridge, and
  deployment boundary without importing Haku Console. The first service path starts a runner, accepts
  an Input, streams events, persists enough history for refresh, and reports failure honestly.
- **Conversation app/UI:** a small client uses the Agentplane API rather than calling provider code
  directly. It shows Thread history, assistant/tool activity, live updates, provisioning/running/
  failed/uncertain states, and refresh-safe completed responses. Generated names and archive
  presentation stay in this app layer.
- **First functioning Agentplane:** one standalone, credentialless end-to-end workflow works for both
  providers with real bridge activity, persisted history, live updates, and an honest terminal result.
- **Secure egress and credentialed readiness:** one narrow synthetic operation proves the fixed sidecar
  to trusted gateway path before any real upstream credential is enabled. Real credentials remain only
  at the gateway; durable freshness/replay, per-Sandbox/Thread binding, rotation, and escape tests gate
  production use.
- **Reliability:** choose one observed failure with the highest user cost after the first functioning
  product. Each hardening slice needs its own reproduction and acceptance test; do not implement every
  candidate in advance.
- **Stretch branches:** collaboration, external events, Haku Console integration, stronger runtimes,
  hardened deployment, and the private-Haku access-control track each begin only after the
  corresponding decision node and dependencies are resolved. The private-Haku track must cover tool
  execution, HTTP/API calls, credential handling, approvals, and policy behavior without assuming that
  routine per-action approval clicks are a reliable safety mechanism.

## Ownership and sequencing rules

- Kubernetes/Agent Sandbox owns Claim, Sandbox, Pod, PVC, readiness, suspension, and workload lifecycle.
- Native harnesses own native history, execution semantics, and provider-native resume.
- Agentplane owns its product records, runner bridge, API/service boundary, and live
  `Pod -> Sandbox -> Thread -> Agent` mapping once that mapping is required.
- The conversation app owns product UX such as generated Thread names, naming persistence, archive
  presentation, and timeline behavior. It may remain separate or later be hosted by Haku Console, but
  must not create shared runtime, route, frontend, or persistence coupling.
- Do not add a common protocol, persistence schema, UI projection, Kubernetes controller, credential
  path, or approval framework to unblock native capture or the shared adapter seam.
- Do not enable real upstream credentials until the secure-egress and credentialed-readiness gates pass.
- A single Thread and one active runner are acceptable for the first functioning product. Multi-Agent
  rooms, subscriptions, external events, advanced retention, and provider migration are not hidden
  prerequisites.

## Detailed plans

- Native provider scenarios and the scripted-test workflow: [`experiments.md`](experiments.md), the
  [native driver README](../native/README.md), the [harness tests README](../harness_tests/README.md),
  and the [live capture probe README](../capture/README.md).
- Sandbox identity and egress evidence: [sandbox spike README](../sandbox-spike/README.md) and [egress
  ADR](adr_sandbox_proxy_gateway.md).
- Product/API layering and deferred capabilities: [`product_surface.md`](product_surface.md) and
  [`architecture.md`](architecture.md).
