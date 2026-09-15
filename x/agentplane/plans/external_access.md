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
- The scoped credential may sit in the Sandbox or be held by a credential-substitution proxy.
  Credential custody is separate from whose authority the target enforces.
- Audit lives in the target's logs and the fence's logs, not in a ledger of named operations.

**Brokered credential.** Haku holds the operator's privileged credential. The agent calls a Haku
tool; policy auto-approves or a human approves; Haku executes with the operator's credential and
returns the result.

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
- **Broker mints delegated grants instead of executing calls.** The grants system already does
  this: `create_grant` asks for exact origins, or namespaces plus verbs, with a duration and a
  rationale, and one approval yields standing authority the target then enforces per call. Approval
  cost is paid once per grant, not once per call.
- **Agent-requested grants.** The agent asks for additional authority on its own identity, the
  operator approves once, and the target enforces from then on. This keeps the apiserver's
  authorization semantics where they belong; the risk is authority issued that cannot be revoked
  in time. See [the candidate revocation gate](#candidate-revocation-gate-for-minted-grants).
- **Broker refuses what delegation already covers.** The Kubernetes redundancy auto-deny is the
  general rule: when the caller's own identity covers a request, the brokered path denies with a
  pointer to the direct path, so the privileged credential is never spent where a scoped one works.

## Kubernetes Sandbox access decisions

`ACCESS` owns the external-system permission model; `SANDBOX_RBAC` owns its Sandbox lifecycle
and UI integration, including individually editable fields that presets may prefill. These are
related tasks, not separate permission systems. The following choices remain open:

- **Credential custody and request path:** does the Sandbox hold a Kubernetes API token and
  call the apiserver directly, or does a proxy substitute a credential that stays outside the
  Sandbox? Specify the token audience, rotation, expiry, exposure, and revocation behavior.
- **Permission authority and enforcement:** manage Kubernetes Roles/ClusterRoles and bindings
  directly; reconcile them from app-owned grant records; or authorize operations in a broker
  using the broker's own Kubernetes principal. In the first two cases Kubernetes enforces the
  Sandbox principal's RBAC; in the third it enforces the broker's authority, so caller-level
  authorization is also the broker's responsibility. Decide whether there is any app-owned
  grant model rather than assuming one, and identify a single desired-state owner for each
  managed object, respecting existing GitOps ownership.

Using a proxy does **not** imply replacing native Kubernetes RBAC: it can substitute a token for
the Sandbox's own principal while the apiserver remains the per-request authorization authority.
Conversely, using Kubernetes RBAC objects does not decide whether desired grants live only in
Kubernetes or are reconciled from an app ledger.

Resolve the principal lifecycle with `SANDBOX_SA` if choosing per-Sandbox ServiceAccounts. Keep
the existing [workload authentication](../docs/workload_authentication.md) boundary distinct from
credentials authorizing Kubernetes API calls; authenticating a Sandbox to Agentplane does not
itself grant Kubernetes access. Any design must explain effective-access inspection, who can
grant or widen permissions, target audit attribution, failure behavior during reconciliation,
and how revocation affects already-issued credentials and requests already in flight. The
proposal below is one candidate, not a settled requirement for `SANDBOX_RBAC`.

## Candidate revocation gate for minted grants

This option assumes app-owned grants reconciled to Kubernetes RBAC and proxy-held credentials.
Its fail-closed and revocation guarantees need evidence, including complete effective-RBAC
accounting and the observation-to-use race; they are not established by this planning document.

Authority in Kubernetes is the token times the bindings, and either can be cut at the apiserver.
"Issued but not revocable" therefore means the reconciler is down or lagging. The guard is a third
cut under Haku's control alone: the agent never holds the real credential, only a placeholder, and
the proxy substitutes the real one only while the identity's RBAC objects match what the ledger
says they should be. This applies the
[sandbox egress identity boundary](../docs/sandbox_egress_identity_evidence.md) to grants.

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
real credential. BuildBuddy is now split at the measured transport boundary: local HTTP API and
gRPC clients can present `agentplane-credential-<name>` as the whole
`x-buildbuddy-api-key` header/metadata value, and the proxy substitutes it across unary and
bidirectional HTTP/2 calls with trailers intact. Under the current header-only contract, `bb remote`
still sends the placeholder in the Bazel command run on BuildBuddy's hosted runner. A narrow rewrite
of the unary `runner.RunRequest.steps[].run` protobuf field can keep the real key out of the local
Sandbox, but it delivers the key to agent-controlled code on the hosted runner; that is a weaker
boundary, not full credentiallessness. The implemented transport contract is canonical in the
[egress SPEC](../egress/SPEC.md); the candidate rewrite and its required evidence are in
[`buildbuddy_remote_auth.md`](../docs/buildbuddy_remote_auth.md).

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

| System                                       | Delegated identity                                                         | Broker needed for                                                       |
| -------------------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Kubernetes                                   | Open: [Sandbox access decisions](#kubernetes-sandbox-access-decisions)     | operations outside delegated authority, subject to broker authorization |
| GitHub                                       | fine-grained token or App installation per repo                            | public-repository policy across search; writes under review             |
| Forgejo                                      | scoped tokens (controller-minted)                                          | nothing identified yet                                                  |
| HTTP egress                                  | fence allowlist by origin; path-level allowlists are the natural extension | origins outside the allowlist                                           |
| BuildBuddy local clients                     | proxy-held key in `x-buildbuddy-api-key` for HTTP and gRPC                 | `bb remote`: choose local-only body rewrite or stronger hosted boundary |
| Gmail                                        | OAuth scopes only                                                          | label-namespace confinement; every mutation                             |
| Others (Matrix, Home Assistant, Tana, Grocy) | unassessed                                                                 | unassessed; default to brokered until assessed                          |

## Consequences for the policy engine

- "Does the caller's own identity already cover this?" is a resolved fact like any other, and the
  redundancy rule applies to every system with a delegated path, not only Kubernetes.
- Policy answers two questions per call: which credential executes it, and whether a human is
  needed. Today only the second is modeled.
- A delegated tool is always-auto by definition, which is what makes it eligible for the
  transparent pass-through schema without a separate policy kind.

## Open questions

- Path-level HTTP allowlists at the egress fence, so "GET on these routes" can be delegated
  without a broker tool per route.
- Whether raw direct traffic should be mirrored into the ledger from fence logs, or stay separate.
- Per-agent identity provisioning per system: what it costs to mint, rotate, and revoke, and where
  the grants system already covers it.
- Which of the unassessed systems have RBAC worth delegating to.
