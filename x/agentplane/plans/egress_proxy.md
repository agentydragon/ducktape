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
- **Policy is custom resources**, editable at runtime through the API server by `kubectl`, the
  app under its RBAC, or an agent's request that Rai approves by flipping a field, and seedable
  from git the way the `SandboxTemplate` is. Two kinds keep the rules DRY when agents' policies
  overlap partially:
  - `EgressPolicy`: a reusable, subject-free rule set. Each rule names hosts (exact, or a `*.`
    suffix), methods, path patterns, and optionally the credential to substitute: the Secret and
    key holding it, the header it goes into, and the placeholder value the sandbox sends.
  - `EgressBinding`: subjects to policies. A subject is a Sandbox by name or by label selector in
    this slice; a thread or an agent identity is a later subject kind on the same resource. A
    binding carries an optional expiry and an approval state (`pending`, `approved`, `denied`,
    with who and when), so an agent's ask is a pending binding and Rai's answer is a field
    change. The proxy writes status: whether the binding is active and which referenced policies
    resolved.
- **Fail closed.** No binding for a subject means no egress; a binding whose policy is missing,
  expired, or not approved contributes nothing and gets a status condition saying so. A denied
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
  garbage-collects the grant; a binding from git carries Flux's inventory labels and nothing at
  runtime touches it, since Flux prunes only what it applied. Every binding also carries a
  provenance label (`agentplane.allegedly.works/granted-by`: `flux`, or the app on whose approval
  it was made) so the view can say "from git" or "granted by Rai at ...". Owner references
  cascade on deletion, not on liveness: nothing is owned by the app's Pod or Deployment, and the
  app going down revokes nothing, because expiry is what fails closed.
- **Time-limited grants need no app in the loop.** `expiresAt` is enforced from the proxy's own
  cache. A lease-style renewal by the app is deliberately not the default: it would put the app
  back into the enforcement path. A per-request use counter was considered and dropped: a count of
  requests says nothing about what they did, so "this once" belongs to the webhook path when a
  rule needs it.
- **Profiles are selector bindings; per-launch picks are per-sandbox bindings.** A preset such
  as "public coder" or "Haku" is a binding in git whose subject is a label selector
  (`agentplane.allegedly.works/profile: public-coder`); launching an agent with that profile
  means stamping the label on the Sandbox, and the Flux-managed binding applies with nothing
  created at runtime. The create form also offers the namespace's individual policies; ticking
  some creates one sandbox-owned binding on top, since bindings are additive. The staging seed is
  the broadest selector binding, every managed sandbox, to be narrowed to a profile once there
  are two kinds of agent.
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
denied for want of a binding, expired binding, unapproved binding, missing policy, copied token,
Secret rotation.

### E2. Resources, RBAC, and the staging seed

The two CRDs with schema validation and printer columns; the proxy's ServiceAccount with read on
the resources and Secrets it needs, status write on bindings, and TokenReview; the app's
ServiceAccount with read on both kinds and write on bindings; the cluster validator covering both;
staging's seed: one approved binding of every managed Sandbox to a policy that lets it reach
GitHub's API and HTTPS git for public repositories with the `agentydragon-agent` PAT substituted.

### E3. Sandbox wiring

The sidecar container and projected token in the `SandboxTemplate`; the runner container's
proxy environment and trusted CA; Cilium policy so only the proxy reaches outside the cluster.
Acceptance: from inside a staging sandbox, a `git ls-remote` of a public repository succeeds
through the proxy with no credential visible in the sandbox, and the same call without the
sidecar's token is refused.

### E4. The app's view and the launch-time pick

Per sandbox: the bindings and resolved rules with their provenance, approval and expiry,
the credentials by name, and the proxy's recent decisions; approve, deny, and revoke through the
API server under the app's RBAC; read-only where the proxy is unreachable. On sandbox creation:
a profile (a label on the Sandbox) and the namespace's individual policies to pick from, the
latter becoming one sandbox-owned binding.

## Left out on purpose

Per-thread and per-agent subjects, the webhook decision path, durable decision history, warm-pool
credential binding, runner-port authentication (the credentialed readiness gate `L` in
[the DAG](task_dag.md)), and any general Agent Console permission model.
