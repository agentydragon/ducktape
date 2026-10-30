# Agent access to external systems

Status: **deferred access-model notes.** Implementation priorities and dependencies live in
[`task_dag.md`](task_dag.md). This file owns the delegated-versus-brokered design choices, including
Kubernetes access for Sandboxes, without making a broad identity, grant, or privilege framework
a prerequisite for individual adapters.

## Two base models

**Delegated identity.** The agent has its own principal in the target system with standing
authority granted once: a Kubernetes ServiceAccount with RBAC, a GitHub App installation or
fine-grained token scoped to repositories, a Forgejo scoped token, or an HTTP allowlist the egress
fence enforces. The target (or the fence) enforces every call; no human is on the call path.

- Enforcement semantics are correct by construction; nothing can be smuggled past a policy that
  reimplements the target's rules, because there is no such policy.
- Least privilege is bounded by the target's RBAC granularity. Kubernetes and GitHub scope well;
  Gmail offers OAuth scopes only.
- Denial is final at the target. Escalation needs a separate path.
- The scoped credential is held by a credential-substitution proxy, never in the Sandbox
  ([principles](../docs/principles.md#strong-outer-sandbox-free-agent-inside-it)). Credential
  custody is separate from whose authority the target enforces.
- Audit lives in the target's logs and the fence's logs, not in a ledger of named operations.

**Brokered credential.** The Action Service holds the operator's privileged credential. The agent
requests an Action; policy auto-approves or a human approves; the Action Service executes with the
operator's credential and returns the result.

- Works for any system, including ones with no usable RBAC.
- Argument-level conditions are possible ("only labels under this namespace", "only repositories
  confirmed public").
- Escalation is built in: manual review is a policy outcome, not a dead end.
- The policy reimplements the target's authorization semantics and is always one API quirk behind;
  the GitHub search `repo:` qualifier is the standing example.
- The broker concentrates credentials and sits on every call's latency path.

## Hybrids

The common case is a mix, split by operation class rather than by system:

- **Reads direct, mutations brokered.** A GET allowlist or read-only RBAC covers the high-volume,
  low-stakes traffic; writes go through the broker with approval.
- **Broker mints delegated grants instead of executing calls.** The former Haku Console grants
  system did this through `create_grant`: one approval yielded standing authority for exact origins
  or Kubernetes namespaces and verbs. That integration has been retired; a replacement broker and
  lifecycle owner would need to be selected before this pattern is available again.
- **Agent-requested grants.** The agent asks for additional authority on its own identity, the
  operator approves once, and the target enforces from then on. This keeps the apiserver's
  authorization semantics where they belong; the risk is authority issued that cannot be revoked
  in time. See [the candidate revocation gate](#candidate-revocation-gate-for-minted-grants).
- **Broker refuses what delegation already covers.** When the caller's own identity covers a
  request, the brokered path denies with a pointer to the direct path, so the privileged credential
  is never spent where a scoped one works. Haku Console's Kubernetes redundancy auto-deny did this
  (removed in #8877); the Action Service has no equivalent yet
  ([TODO](../action_service/TODO.md#refuse-an-action-the-callers-own-permissions-already-cover)).

## Kubernetes Sandbox access decisions

[#8596](https://github.com/agentydragon/ducktape/issues/8596) selected one ServiceAccount
per managed Sandbox. Its egress sidecar holds a projected Kubernetes-audience token and
substitutes that Sandbox SA's identity for `kubectl` requests. The workload receives a
placeholder kubeconfig, not the token. Kubernetes enforces the resulting SA's RBAC.

[Agent RBAC](../../cluster/docs/agent_rbac.md#agentplane-managed-sandbox-grants) documents the
implemented grant catalog, launch-time resolution and binding ownership, and the
[PolicySet design](../docs/adr_policy_sets.md) covers runtime grant changes.

External OAuth Haku continues to use its static `claude-ai` caller through MCP-created
sandboxes. It runs `kubectl` from that sandbox; no OAuth-to-Kubernetes gateway is part
of this design. Its existing static permissions and view-only Coinbase Secret grant
remain separate from the managed Haku preset. Live acceptance still required is
[the Sandbox task](task_dag.md#sandbox_rbac--bounded-deployed-grant-checks).

## Candidate revocation gate for minted grants

This option assumes app-owned grants reconciled to Kubernetes RBAC and proxy-held credentials.
Its fail-closed and revocation guarantees need evidence, including complete effective-RBAC
accounting and the observation-to-use race; they are not established by this planning document.

Authority in Kubernetes is the token times the bindings, and either can be cut at the apiserver.
"Issued but not revocable" therefore means the reconciler is down or lagging. The guard is a third
cut under Agentplane's control alone: the agent never holds the real credential, only a placeholder, and
the proxy substitutes the real one only while the identity's RBAC objects match what the ledger
says they should be. This applies the egress gateway's
[identity boundary and evidence](../docs/adr_sandbox_proxy_gateway.md#identity-boundary-and-historical-evidence)
to grants.

The gate compares object sets, never requests, so the proxy needs no knowledge of API paths:

- **Desired:** the RoleBindings, and the Roles they reference, that the ledger's active grants imply
  for this identity.
- **Actual:** every binding in the apiserver whose subjects include the identity, indexed by subject
  rather than by our own label, so a binding added by hand is seen. Content is compared (`roleRef`,
  subjects, the referenced Role's rules), not names, so a widened Role is a mismatch.
- **Equal:** substitute the credential; the apiserver does all per-request authorization.
- **Not equal, in either direction:** refuse substitution with "permissions are reconciling, retry
  later", even for a request both sides would allow. An extra binding after a revocation is a
  mismatch, so lag can never leave authority usable. Unknown state (stale cache, apiserver
  unreachable) refuses too; that costs availability, never safety.

Issue and revoke are then plain ledger writes followed by reconciliation, with the proxy opening
or closing on its own as the two sides converge. Real tokens come from TokenRequest with a short
TTL the proxy refreshes, so nothing long-lived exists to leak, and the placeholder is bound to the
sandbox identity so it is useless elsewhere.

One identity per grant is an availability optimization, not a correctness requirement: with a
single identity, revoking any grant pauses all of the agent's Kubernetes access until the
reconciler lands; with one identity per grant, only the grant being revoked pauses.

The same guarantee holds for any target the egress fence fronts, since header substitution works
the same for GitHub or Forgejo tokens, with the desired-versus-actual check replaced by whatever
that target exposes about the token's scope; it does not hold where the agent must possess the
real credential. BuildBuddy-hosted `bb remote` is such a case: the
[egress SPEC](../egress/SPEC.md) covers local HTTP/gRPC clients, and
[`buildbuddy_remote_auth.md`](../docs/buildbuddy_remote_auth.md) records why the hosted runner
still needs the real key.

## Choosing

Delegate whenever the target's RBAC or a simple allowlist can express the boundary: a set of
routes and methods, a namespace and verb set, a repository set. Broker only when the boundary is
something the target cannot express, such as a label namespace inside one mailbox or "public
repositories only" across GitHub search. When in doubt, ask whether a wrong policy edit would let a
call through that the target itself would have refused; if the target would refuse it anyway, the
policy is redundant and delegation is the answer.

## Why MCP tools stay in the picture

Packaging an operation as a named tool with a schema is for the human, not the security boundary.
`POST /api/v1/dispmanager_rpc/1277` with a JSON blob is unreviewable; "set thermostat schedule
{room, weekday, temperature}" renders as a card the operator can approve in a glance and the ledger
can search later. That value is independent of which credential executes the call.

So the tool catalog is a presentation and audit contract, and the credential is a per-tool
property:

| Tool class | Executes as                       | Approval mode                 | Ledger          |
| ---------- | --------------------------------- | ----------------------------- | --------------- |
| delegated  | the agent's own identity          | always auto (target enforces) | yes             |
| brokered   | the operator's credential         | policy or manual              | yes             |
| raw direct | the agent's own identity, no tool | none (fence enforces)         | fence logs only |

Delegated tools keep the uniform result envelope and the cards while adding no human step. Raw
direct access is for traffic where cards would be noise, such as bulk reads, and its audit is the
fence's request log. A system can offer all three at once; the roster per system says which
operations fall where.

## Per-system inventory

| System                                       | Delegated identity                                                                 | Broker needed for                                                       |
| -------------------------------------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Kubernetes                                   | one ServiceAccount per Sandbox ([decisions](#kubernetes-sandbox-access-decisions)) | operations outside delegated authority, subject to broker authorization |
| GitHub                                       | fine-grained token or App installation per repo                                    | public-repository policy across search; writes under review             |
| Forgejo                                      | scoped tokens (controller-minted)                                                  | nothing identified yet                                                  |
| HTTP egress                                  | fence allowlist by host, method, and path                                          | requests outside the allowlist                                          |
| BuildBuddy local clients                     | proxy-held key in `x-buildbuddy-api-key` for HTTP and gRPC                         | `bb remote`: choose local-only body rewrite or stronger hosted boundary |
| Gmail                                        | OAuth scopes only                                                                  | label-namespace confinement; every mutation                             |
| Others (Matrix, Home Assistant, Tana, Grocy) | unassessed                                                                         | unassessed; default to brokered until assessed                          |

## Compatibility evaluation: GitHub, Kubernetes, HTTP

**Deferred evaluation, separate from multiagent implementation.** Revisit when a concrete missing
capability or demonstrated maintenance cost motivates broader auth changes. Multiagent permission
design tentatively selects [service-enforced Session-access PolicySet rules](multiagent_communication.md#tentative-implementation-direction)
on the existing SA/policy foundation, subject to revision if implementation does not work well.
It does not wait for this evaluation or an engine migration.
Any wider adoption proposal is independently reviewable and must identify its benefit, scope and
migration cost. No new service or identity-platform migration is selected here.

The operator is also open to reducing the app's special auth handling, but accepts keeping its
existing path for multiagent v1. Review any simplification separately when a concrete benefit or
operator prioritization motivates it; no common replacement path is selected or required.

### Compare against Agentplane's existing auth design

Use the [policy-set decisions](../docs/adr_policy_sets.md) and
[design principles](../docs/principles.md) as the baseline:

- **Separate shared definitions from enrollment.** Preserve subject-free, mutable permission
  bundles assigned independently to static and runtime-created SAs. Additions and removals reach
  every current assignee, across Kubernetes, egress and Actions. Overlapping grants remain additive;
  any proposed deny semantics are an explicit change, not an incidental effect of engine adoption.
- **Retain verified identity and configurable topology.** Use the existing SA attribution and
  explicit OAuth Connection-to-SA mapping. Session/Sandbox targets and optional relationships are
  policy inputs; teams, management hierarchies and new agent identities are not prerequisites.
  State who may author policy, enroll accounts and supply or change each trusted relationship.
- **Preserve useful rule semantics.** Exercise existing Role/ClusterRole references and namespace
  scopes, egress destination/credential rules, and argument-sensitive Action auto-approval. A shared
  evaluator must preserve each service's contract, including human approval and execution evidence.
- **Own lifecycle and consistency.** Identify the authoritative owner of each definition,
  assignment and resource fact; explain any projections into engine data or native bindings.
  Address runtime grants alongside GitOps, expiry without recreation, SA replacement, pruning,
  stale replicas and policy-version evidence. State propagation and outage behavior explicitly.
- **Account for composition and cost.** Keep backends usable with the integration app unavailable
  and communication optional. Compare required services, state stores, adapters, custom evaluator
  code, operator configuration and reconciliation work. Retaining today's CRDs is not mandatory;
  a replacement must preserve their useful semantics and justify the total change.

Run the same concrete cases for each candidate: enroll ten mixed static/runtime SAs in one bundle;
change thirty native bindings per SA to twenty-five; temporarily add and remove a shared egress
rule; preserve an overlapping grant; enroll a new Sandbox SA without rewriting existing members.
Also express independent send/read/control rights on particular targets and bounded launch/model
arguments. Show the configuration edits and which need data writes, engine-model/schema changes,
application changes or workload restarts. A relationship demo or a list of supported features is
not a fit assessment.

### Candidate mappings to evaluate

These are starting hypotheses from primary documentation, not completed compatibility or adoption
results. Compare each with extending the current design before recommending a broader change.

| Candidate                                                          | Starting mapping                                                                                                                          | Agentplane-specific fit to establish                                                                                                                                                                   |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Extend the current `PolicySet`/`PolicyBinding` design              | Keep shared typed rules, independent SA assignments and service-specific readers; add the required operation/target scopes.               | Can the new scopes, discovery and relationship inputs stay understandable? Compare the resulting custom code and maintenance cost with the alternatives.                                               |
| [OpenFGA](https://openfga.dev/docs/modeling/roles-and-permissions) | Map SAs, bundle enrollment and resource permissions into its authorization model and relationship data.                                   | Show mutable bundle definitions without per-assignee rule copies, distinguish tuple edits from model deployments, and account for trusted writers, consistency and native Kubernetes projections.      |
| [SpiceDB](https://authzed.com/docs/spicedb/concepts/schema)        | Map the same facts into schema-defined relations and permissions.                                                                         | Show shared definition edits, contextual constraints, discovery and native grant reconciliation; specify schema/data lifecycle and freshness requirements for backends, streams and queued operations. |
| [Cedar](https://docs.cedarpolicy.com/overview/terminology.html)    | Map verified SAs, operations, targets and trusted context into policy requests, with shared policies and separate enrollment/entity data. | Demonstrate live assignment semantics, authoritative entity inputs, authorized list/search integration and native Kubernetes enforcement; do not silently introduce deny behavior.                     |
| [OPA](https://www.openpolicyagent.org/docs/external-data)          | Evaluate requests against policy plus authoritative grant, assignment and resource data.                                                  | Choose data ownership/distribution and freshness, preserve the typed service contracts, and show that the remaining integration code and operational cost improve on the baseline.                     |

Authorization services bundled with identity platforms such as
[Keycloak](https://www.keycloak.org/docs/latest/authorization_services/index.html) are further
candidates under the same criteria. Authentication and authorization remain distinct even when one
product implements both. Compare authoring, storage, evaluation and enforcement separately;
using a relationship engine or policy language does not itself select Kubernetes versus Git versus
runtime authoring, or remote versus locally distributed evaluation.

SpiceDB's [freshness tokens](https://authzed.com/docs/spicedb/concepts/consistency) and OpenFGA's
[consistency modes](https://openfga.dev/docs/interacting/consistency) are mechanisms to assess, not
automatic solutions for application-level stream or queue revocation. A common decision interface
still requires enforcement in each resource-owning backend and native grants or a deliberate
integration for Kubernetes API access.

### Decision protocol and system probes

Evaluate the decision protocol separately from the engine:
[OpenID AuthZEN](https://openid.net/specs/authorization-api-1_0.html) standardizes subject, action,
resource, and context queries, with a boolean decision and optional response context whose semantics
are implementation-defined. It is a candidate interface, not a complete grant or execution protocol.
In particular, it does not itself standardize our distinction between denied, awaiting human
approval, and approved but not executed. Preserve the
[Action Service's durable approval/execution contract](../action_service/SPEC.md) and identify any
necessary protocol extension or separate workflow explicitly. Do not report a pending approval as
an executable permission or treat a decision as proof that an operation ran.

Initial system-specific probes:

- **GitHub:** repository-scoped reads, public-only operations/search, and approved mutations.
  [GitHub App installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
  can be narrowed by repository and permission within the installation's authority. Determine which
  restrictions those native scopes can express and which need checks in our proxy/executor, including
  operation arguments and facts fetched from GitHub. Do not assume GitHub consults our decision
  service. Compare direct scoped credentials with proxy-held credentials and prove that broader
  credential authority cannot bypass the additional checks.
- **Kubernetes:** namespace-scoped reads, denied writes/cross-namespace requests, and temporary
  elevation. Compare the [open Sandbox access choices](#kubernetes-sandbox-access-decisions) with
  a native [authorization webhook](https://kubernetes.io/docs/reference/access-authn-authz/webhook/).
  Test [authorizer composition and ordering](https://kubernetes.io/docs/reference/access-authn-authz/authorization/):
  installing a webhook does not automatically make its approval mandatory on every request.
  Separate request-attribute authorization from object-body constraints requiring admission or
  another enforcement point. Native RBAC objects, an app grant ledger, and credential custody
  remain separate choices.
- **HTTP:** authenticated Sandbox identity plus canonical destination, method, and path checks,
  with destination-bound credential substitution. Start from the existing
  [egress contract](../egress/SPEC.md); this is not a proposal to add already-supported path matching.
  Determine when URL-level checks suffice and when body/domain knowledge is needed. Test redirects,
  alternate URL encodings, credential leakage to another destination, and direct paths around the
  enforcing proxy. [OPA's HTTP integration](https://www.openpolicyagent.org/docs/http-api-authorization)
  is one example of request-time decision integration, not a complete credential boundary.

For each probe, distinguish request-time decisions from provisioning/revoking target-side grants.
Identify a single desired-state owner for each grant and specify how multiple restrictions combine.
Compare remote per-request evaluation with centrally administered, locally distributed policy.
Evidence must cover trusted input provenance, stale caches, policy changes between decision and
execution, expiry/revocation (including already-issued credentials and in-flight work), dependency
outages, fail-closed behavior, bypass attempts, and audit evidence sufficient to explain the decision.
Bind human approval to the reviewed operation and arguments. Do not silently invent new failure or
retry semantics for existing egress or Action callers.

The output is a per-system fit assessment with executable positive/negative cases, explicit gaps,
and an authority/enforcement map. Adopt a hybrid only where it has a clear owner at each boundary,
not two competing permission stores. No product, new service, or identity-platform migration is
selected by this plan.

## Consequences for the policy engine

- "Does the caller's own identity already cover this?" is a resolved fact like any other, and the
  redundancy rule applies to every system with a delegated path, not only Kubernetes.
- Policy answers two questions per call: which credential executes it, and whether a human is
  needed. Today only the second is modeled.
- A delegated tool is always-auto by definition, which is what makes it eligible for the
  transparent pass-through schema without a separate policy kind.

## Open questions

- Whether raw direct traffic should be mirrored into the ledger from fence logs, or stay separate.
- Per-agent identity provisioning per system: what it costs to mint, rotate, and revoke, and which
  active products already cover it. Haku Console grants previously covered only a subset of these
  cases and are retired.
- Which of the unassessed systems have RBAC worth delegating to.
