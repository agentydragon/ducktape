# Secure egress integration

Status: **in progress**; the packages below are the burn-down. The shape was decided in
[the ADR](adr_sandbox_proxy_gateway.md): the sandbox holds no real credential, a per-Pod sidecar
carries only a projected, audience-scoped ServiceAccount token to a central proxy, and the proxy
verifies that token against Kubernetes, correlates the live Pod with its Sandbox, applies policy,
and substitutes the real credential. This document adds where policy and credentials live, how the
proxy relates to the integration app, and the work packages.

## Decisions

- **The proxy depends on the API server only.** The integration app is an aggregator that
  presents a view of everything; nothing runs through it. The proxy reads its policy from
  Kubernetes, verifies tokens against Kubernetes, and keeps its own decision log. The app reads the
  same custom resources and asks the proxy for recent decisions; with the app down the proxy is
  unaffected, and with the proxy down the app still shows the rules.
- **Policy is custom resources**, editable at runtime through the API server by `kubectl` or the
  app under its RBAC, and seedable from git the way the `SandboxTemplate` is. Two kinds keep the rules DRY when agents' policies
  overlap partially:
  - `EgressPolicy`: a reusable, subject-free rule set. Each rule names hosts (exact, or a `*.`
    suffix), methods, path patterns, and optionally the credential to substitute: the Secret and
    key holding it, the header it goes into, and the placeholder value the sandbox sends.
  - `EgressBinding`: subjects to policies. A subject is one Sandbox, by name; a thread or an agent
    identity is a later subject kind on the same resource. The
    binding's existence is the permission and its optional expiry is the only thing that ends one
    without deleting it: creating the object is the whole act of allowing, so there is no decision
    field to answer it with. The proxy writes status: whether the binding is active and which
    referenced policies resolved.
- **Fail closed.** No binding for a subject means no egress; a binding that is expired or whose
  policy is missing contributes nothing and gets a status condition saying so. A denied
  request is answered with `403` and a short machine-readable reason header, never with upstream
  detail.
- **Credentials stay Secrets**, managed as today (ESO or SOPS through GitOps) and mounted only
  into the proxy Pod. A rule references a credential by Secret name and key; rotation is a
  GitOps concern and the proxy re-reads on change.
- **Decisions are the proxy's own**: a bounded in-memory ring per subject plus structured logs,
  served on a cluster-internal read endpoint the app queries. Durable decision history, if ever
  wanted, is the proxy's own database.
- **Webhooks are the escape hatch, not the first move.** A rule may later delegate its decision
  to a webhook for cases a static rule cannot express; nothing is built for it until a rule
  needs it.
- **Subject identity is the Sandbox** in this slice, because that is what the Pod-bound token
  proves. Threads and agents attach later as columns, not as a redesign.
- **Runtime grants die with their subject and say where they came from.** A binding the app
  creates for one sandbox carries an `ownerReference` to that Sandbox, so deleting the sandbox
  garbage-collects the grant; a binding from git carries Flux's inventory labels
  (`kustomize.toolkit.fluxcd.io/name`), which is what the view reads to say "from git" and what
  the app refuses to revoke, since Flux prunes only what it applied and would re-apply it on the
  next reconcile. Who created a runtime binding is not re-asserted as a label of our own: creating
  one is the whole grant, so everyone who may create one is equally entitled, and the API server's
  audit log and `managedFields` record the actor better than we could. Owner references
  cascade on deletion, not on liveness: nothing is owned by the app's Pod or Deployment, and the
  app going down revokes nothing, because expiry is what fails closed.
- **Time-limited grants need no app in the loop.** `expiresAt` is enforced from the proxy's own
  cache. A lease-style renewal by the app is deliberately not the default: it would put the app
  back into the enforcement path. A per-request use counter was considered and dropped: a count of
  requests says nothing about what they did, so "this once" belongs to the webhook path when a
  rule needs it.
- **Every grant is a per-sandbox binding.** The create form offers the namespace's policies;
  ticking some creates one sandbox-owned binding, and a sandbox nothing names reaches nothing.
  There is no standing rule over a class of sandboxes: presets such as "public coder" or "Haku"
  are the deferred profile concept ([`profiles.md`](profiles.md)), which must not re-enter as a
  selector on this CRD.
- **Credentials live in their own namespace.** The proxy reads Secrets only from
  `agentplane-egress-credentials`, where the ExternalSecrets for substituted credentials are
  delivered; a rule's `secretRef` resolves there. RBAC cannot filter Secrets by label, and a
  namespace-wide read in the sandbox namespace would also expose the LiteLLM key and the
  database credential to the proxy.
- **Transport**: the sandbox's tools use an ordinary HTTPS proxy at the sidecar; the sidecar
  relays to the central proxy and adds the Pod token on every hop; the central proxy terminates
  TLS with a CA the runner container trusts (the trust-manager bundle pattern
  `cluster/k8s/agents/haku-egress-proxy/` already uses), applies the rule, substitutes the
  header, and re-issues the request upstream. Haku's egress fence (mitmproxy plus `iron-proxy`
  placeholder substitution) is the prior art for the interception and substitution mechanics;
  Agentplane's proxy is its own code with the Pod-token identity and the resource-driven policy,
  and may build on mitmproxy's engine rather than hand-rolling TLS interception.

## Work packages

Independent PRs; packages 1 and 2 need only the resource schema above, and start in parallel.

### E1. Central proxy

TokenReview of the sidecar's token; the live Pod UID and IP to Sandbox owner lookup; an informer
(a list-and-watch with a local cache that keeps the proxy's picture of the resources equal to the
API server's) over `EgressPolicy`, `EgressBinding`, and the referenced Secrets; rule evaluation;
credential substitution; the decision ring and its read endpoint; status written back to
bindings. Tested against a fake API server and a scripted upstream: accepted, denied by rule,
denied for want of a binding, expired binding, missing policy, copied token, Secret rotation.

### E2. Resources, RBAC, and the staging seed

The two CRDs with schema validation and printer columns; the proxy's ServiceAccount with read on
the resources and Secrets it needs, status write on bindings, and TokenReview; the app's
ServiceAccount with read on both kinds and write on bindings; the cluster validator covering both;
staging's `github-public` policy, which lets a sandbox bound to it reach GitHub's API and HTTPS
git for public repositories with the `agentydragon-agent` PAT substituted.

### E3. Sandbox wiring

The sidecar container and projected token in the `SandboxTemplate`; the runner container's
proxy environment and trusted CA; Cilium policy so only the proxy reaches outside the cluster.
Acceptance: from inside a staging sandbox, a `git ls-remote` of a public repository succeeds
through the proxy with no credential visible in the sandbox, and the same call without the
sidecar's token is refused.

### E5. The model endpoint through the proxy too

A runner Pod carries the `cheap-experiments` LiteLLM key as environment and hands it to every
harness child, and `NO_PROXY` names the LiteLLM host so that traffic bypasses the sidecar. That is
the one real credential left in a sandbox, and the design does not require it there: the key opens
an external system like any other, and holding those is what the proxy is for. The app's README
calls it a staging-first convenience, "the same operational convenience the `agent-workspaces`
Codex lane uses"; this package is what retires it.

The end state is the shape every other credential already has. The harness gets a placeholder in
place of the key, LiteLLM's host leaves `NO_PROXY` so model traffic takes the sidecar like
everything else, and a rule substitutes the real key from a Secret in the credentials namespace.
An agent that reads its own environment then finds nothing worth stealing, and a compromised
harness cannot spend the budget except through a proxy that records every call.

The runner change that enables it is a per-harness form of `--harness-env`: the provider
credential stops being `os.environ["ANTHROPIC_AUTH_TOKEN"]` read by the runner and becomes a value
the deployment declares for that harness, which is what lets it be a placeholder instead of a key.
`--anthropic-base-url` can go the same way, since Claude's endpoint reaches it only as
`ANTHROPIC_BASE_URL`. Two things do not follow:

- **Codex's endpoint is not only an environment variable.** `native/codex/scenarios.command()`
  builds `-c` config overrides from it as well as setting `OPENAI_BASE_URL`, so `--openai-base-url`
  cannot become an env flag without generalizing those overrides too.
- **`CLAUDE_CONFIG_DIR` and `CODEX_HOME` cannot move.** They are `session.directory / <harness>`,
  computed per session by the runner, and no deployment can name them.

Open questions to answer before it is worth doing:

- **Latency and streaming.** Model traffic is long-lived streaming SSE, unlike the request-shaped
  calls the proxy handles today. Whether interception costs anything that matters on a token
  stream is a measurement nobody has taken.
- **Whose budget.** The key is a per-instance budget cap and kill switch. Once the proxy
  substitutes it, a rule decides which sandbox spends which key -- finer-grained than one key per
  deployment, and possibly its own resource shape.
- **What a proxy outage costs.** Decided: accept it, and put the proxy's own availability on the
  board rather than in this package. Model traffic bypassing the proxy is why a sandbox keeps
  working across a proxy restart, so routing it through makes the proxy a hard dependency of every
  turn. What makes that more than a shrug is that the proxy is single-replica _by design_: the
  decision ring is per-process in-memory state and the app reads `/decisions` through a Service
  selecting every pod, so raising replicas splits the view the app and the acceptance suite assert
  on. Availability and the decision view are one problem, and it is node `PR` in
  [the DAG](task_dag.md), not a line in this one. Staging tolerates it: sandboxes are ephemeral and
  a turn spanning an egress roll is a broken turn, not lost state.

## Left out on purpose

Per-thread and per-agent subjects, the webhook decision path, durable decision history, warm-pool
credential binding, runner-port authentication (the credentialed readiness gate `L` in
[the DAG](task_dag.md)), and any general Agent Console permission model.
