# Agent access to external systems

Status: **design notes feeding the access-control decisions (`AA`, `AB`) in
[`task_dag.md`](task_dag.md).** One vocabulary for how an agent reaches a third-party system,
the rule for choosing, and what each choice costs. Companion to
[`async_approvals.md`](async_approvals.md), which covers the brokered path's approval protocol.

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
- The credential sits in the sandbox, which is fine because it is the agent's own and scoped; the
  sandbox is the blast radius.
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
  in time. See § Revocation guarantee for minted grants.
- **Broker refuses what delegation already covers.** The Kubernetes redundancy auto-deny is the
  general rule: when the caller's own identity covers a request, the brokered path denies with a
  pointer to the direct path, so the privileged credential is never spent where a scoped one works.

## Revocation guarantee for minted grants

Authority in Kubernetes is the token times the bindings, and either can be cut at the apiserver.
"Issued but not revocable" therefore means the reconciler is down or lagging. The guard is a third
cut under Haku's control alone: the agent never holds the real credential, only a placeholder, and
the proxy substitutes the real one while Haku's ledger says the grant is active. This is the
sandbox spike's proxy-only secret delivery applied to grants.

The gate must not depend on scope matching, or the proxy ends up reimplementing the apiserver's
path-to-resource mapping. On revocation, live RBAC may still be broader than the ledger, so a
SubjectAccessReview cannot tell a still-valid request from a revoked one. Minting one ServiceAccount
and token per grant removes the need:

- **Issue:** create the ServiceAccount and bindings; enable substitution only once a
  SubjectAccessReview for a probe request inside the grant's scope answers yes, so the agent never
  sees reconciliation-lag 403s.
- **Use:** the agent holds one placeholder per grant, bound to the sandbox identity so it is useless
  elsewhere; the proxy swaps in that grant's token. The apiserver does all authorization.
- **Revoke:** flip the ledger first, which stops substitution at once; delete the ServiceAccount and
  bindings afterward, with an alarm if they outlive a grace period. Reconciliation lag is harmless
  because the credential is already dead to the agent.

Real tokens come from TokenRequest with a short TTL the proxy refreshes, so nothing long-lived
exists to leak. The same guarantee holds for any target the egress fence fronts, since header
substitution works the same for GitHub or Forgejo tokens; it does not hold where the agent must
possess the real credential. The one known case is BuildBuddy, whose API key rides inside the
Bazel gRPC protocol as a remote header rather than at the HTTP edge, so the fence cannot substitute
it and the agent holds the real key. Accepted: the key is low-sensitivity and unresolved rather
than unresolvable.

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

| System                                       | Delegated identity                                                         | Broker needed for                                           |
| -------------------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Kubernetes                                   | one ServiceAccount per grant, proxy-substituted; SAR-checked               | anything the agent's RBAC does not cover                    |
| GitHub                                       | fine-grained token or App installation per repo                            | public-repository policy across search; writes under review |
| Forgejo                                      | scoped tokens (controller-minted)                                          | nothing identified yet                                      |
| HTTP egress                                  | fence allowlist by origin; path-level allowlists are the natural extension | origins outside the allowlist                               |
| Gmail                                        | OAuth scopes only                                                          | label-namespace confinement; every mutation                 |
| Others (Matrix, Home Assistant, Tana, Grocy) | unassessed                                                                 | unassessed; default to brokered until assessed              |

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
