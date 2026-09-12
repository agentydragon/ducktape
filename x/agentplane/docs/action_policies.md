# Action policies

How the single operator configures bounded auto-approval for both caller classes of the Action
Service: external MCP Connections and harnesses running in Sandbox Threads. The contract is in the
[Action Service specification](../action_service/SPEC.md) § Action policies, the implementation in
its [README](../action_service/README.md) § Action policy sets and bindings, the integration app's
binding writer and Sandbox view in the [app README](../app/README.md) § Action policy, and presets
in [launch presets](launch_presets.md). This document records the model's shape and the reasons
for it.

## Model

Two namespaced Kubernetes resources in `agentplane.allegedly.works/v1alpha1`
(`cluster/k8s/agentplane-crds/`), watched by the Action Service, plus ordinary ServiceAccounts as
the external-caller principal.

| Kind                  | Spec                                                                                               |
| --------------------- | -------------------------------------------------------------------------------------------------- |
| `ActionPolicySet`     | `autoApproveIf`, `autoDenyIf`, `autoDenyUnless`: lists of typed policies                           |
| `ActionPolicyBinding` | `subject` (`serviceAccount {namespace, name}` or `sandbox {name, uid}`), `policySets`, `expiresAt` |

**An external caller is a Kubernetes ServiceAccount.** One principal then carries native
permissions through ordinary RoleBindings and Action permissions through an `ActionPolicyBinding`,
and Sandbox callers are already ServiceAccount-backed workload principals, so there is one identity
vocabulary. The label `agentplane.allegedly.works/action-caller: "true"` makes a ServiceAccount
eligible: MCP clients speak OAuth, so the app's consent UI lists the eligible ServiceAccounts and the
operator picks one at consent. A missing or unlabeled ServiceAccount refuses resolution at admission
and at the dispatch claim; removing the label or the object is the disable.

**A policy is one typed evaluator.** YAML carries `type` and parameters, Python owns the semantics
(one module per kind under `action_service/policies/`). A constraint the YAML cannot express is a
new kind, never a DSL. Kinds that consult state outside the arguments arrive with the Actions that
need them, as `github_public_repository` did with its live visibility lookup.

**A policy set is the shared unit** and the only thing a subject references; a one-off grant is a
small set of its own. `autoApproveIf` auto-approves a matching request, `autoDenyIf` auto-denies
one, `autoDenyUnless` auto-denies a request matching none of its policies; deny wins over approve,
and a request matching nothing takes the human path. Only `autoApproveIf` decides today
(`DENY_LISTS` in the [task DAG](../plans/task_dag.md)).

**A binding joins one subject to sets, by reference only.** A subject may have many bindings; the
effective policy is the union of the unexpired bindings' sets. `expiresAt` makes an expired binding
equivalent to an absent one. Sandbox subjects pin the live UID, so a binding whose Sandbox is gone
is inert. No caller-controlled field selects a binding: subjects resolve from the authenticated
workload principal or the Connection's ServiceAccount, never from `origin`, `correlation`, a Thread
ID, or a claimed type.

## Ownership

Each object is owned by Git through Flux or by a runtime writer (the integration app, or the
operator with `kubectl`); the split is per object, never per kind.

- Policy sets and caller ServiceAccounts normally live in Git next to the environment's Action
  Service settings (`cluster/k8s/agentplane-staging/actions/`); one created at runtime is simply
  not Flux-owned.
- The integration app writes one `ActionPolicyBinding` per Sandbox it launches, alongside the
  `EgressBinding`, with an `ownerReference` to the Sandbox so both are garbage collected with it.
  Which sets a preset selects is the app's knowledge, kept in its own labels and annotations; the
  Action Service reads `spec` only and never sees preset language.
- Widening one Sandbox later is another binding for the same subject, written through the app or
  `kubectl`, usually with `expiresAt`. Re-resolving a preset touches only the bindings the app owns.
- ServiceAccount bindings are ordinarily Git-managed; a runtime, expiring one works the same way.

## Evaluate once

Policies are evaluated at admission against the objects as they are then, and never again for that
Action: a later edit, expiry or deletion changes the next Action's Decision, not this one's, the
same as a human approval is not withdrawn by a later change of mind. Dispatch keeps the
caller-authority checks (an active grant, an eligible ServiceAccount) and adds no policy
re-evaluation; stopping approved-but-unclaimed work is an operator cancel, not a policy concern.
The Decision records binding names and `resourceVersion`s, set generations, and the leaf policy
that matched, so it explains itself after the objects change. Until the informer has synced, every
caller is human-only; an object that fails validation contributes nothing and reports `Ready=False`
with the message in its status, so a bad runtime edit is visible in `kubectl get`.

## Example

Staging's Git-owned half is `actionpolicyset-github-reads.yaml`, `serviceaccount-claude-ai.yaml`
and `actionpolicybinding-claude-ai-github-reads.yaml` under
`cluster/k8s/agentplane-staging/actions/`. The runtime half, for a Sandbox `coder-7f3a` launched
from a preset naming `github-reads`:

```yaml
# Written by the integration app at launch.
apiVersion: agentplane.allegedly.works/v1alpha1
kind: ActionPolicyBinding
metadata:
  generateName: coder-7f3a-
  namespace: agentplane-staging
  labels: { app.agentplane.allegedly.works/managed-by: integration-app }
  ownerReferences:
    - { apiVersion: agents.x-k8s.io/v1beta1, kind: Sandbox, name: coder-7f3a, uid: 2c1d9e1a-… }
spec:
  subject: { sandbox: { name: coder-7f3a, uid: 2c1d9e1a-… } }
  policySets: [github-reads]
---
# The operator widens that one Sandbox for the afternoon.
apiVersion: agentplane.allegedly.works/v1alpha1
kind: ActionPolicyBinding
metadata:
  name: coder-7f3a-ducktape-push-20260912
  namespace: agentplane-staging
  ownerReferences: [same owner]
spec:
  subject: { sandbox: { name: coder-7f3a, uid: 2c1d9e1a-… } }
  policySets: [ducktape-push]
  expiresAt: "2026-09-12T20:00:00Z"
```

A `search_code` auto-approves for the Sandbox and for a Connection acting as `claude-ai` alike;
removing that action from `github-reads` makes the next one wait for the operator for both, with no
binding edited; at 20:01 the push grant is gone and the Decisions it produced still name the
binding revision they used.

## Rejected

- **A policy DSL or expression language.** Every constraint is a reviewed Python evaluator with a
  typed wire model; the YAML can only select and parameterize kinds, so what a set can grant is
  bounded by code review, not by what an expression can reach.
- **An `identities:` map in the Action Service settings as the external principal.** It was a
  second identity vocabulary beside the Sandbox's ServiceAccount-backed one, with its own
  enable/disable and no native permissions; a labeled ServiceAccount is one principal for both.
- **PostgreSQL or app configuration as the policy store.** Ownership is per object: Git through
  Flux for the reviewed sets and callers, a runtime writer for per-Sandbox bindings that must be
  garbage collected with the Sandbox. Kubernetes objects give both writers one authority, an
  `ownerReference` cascade, and a `Ready` condition for validation feedback; either alternative
  needs a second sync path for one of the writers.
- **Preset language on the binding** (a `<sandbox>-<preset>` name or a preset field). The service
  would then hold something only the app defines and nothing reads; the binding carries only the
  app's managed-by label, and the preset stays on the Sandbox's own annotation.
- **Re-evaluating policy at dispatch.** An approval a later object edit could withdraw is a
  different contract from a human approval; dispatch re-checks caller authority only.
