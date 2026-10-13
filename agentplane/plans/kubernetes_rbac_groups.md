# Kubernetes grants through PolicySets: what remains

The model, its decisions and the rejected alternatives are in
[Policy sets](../docs/adr_policy_sets.md). The [Policy Controller](../policy_controller/SPEC.md)
runs on staging and `agentplane-testing`; the Sandbox Controller writes each Sandbox's
`PolicyBinding`; presets and the launch form name only `PolicySet`s; cdk8s renders every grant
catalog entry as a set. The [task DAG](task_dag.md) tracks the remaining nodes below.
[Agent RBAC](../../cluster/docs/agent_rbac.md) describes deployed scope.

## Runtime grants (`POLICY_RUNTIME_GRANTS`)

An authorized operator can grant or revoke one set for one existing ServiceAccount, with an
optional `expiresAt`, without changing shared sets or other accounts. A grant is a second
`PolicyBinding` for the account, so overlapping grants combine additively and revoking one leaves
the other. The Sandbox Service writes such bindings today, for egress only, owned by the Sandbox.
The direction (operator, 2026-10-13 PDT): the app makes the Kubernetes call for the operator who
asked, for sets of any rule type; the layers that execute on the CRs act on them and do not edit
them, so the Sandbox Service's grant and revoke RPCs go.

- An account cannot grant itself access by naming a powerful set: only the operator-facing app
  writes bindings, and no agent holds write access to `PolicyBinding`s. Who is recorded for each grant or revoke is
  still to be defined.
- The operator can inspect each binding's sets, origin, expiry and readiness conditions. Expanded
  per-namespace scope stays out of the status payload; native bindings carry the binding's name as
  an annotation.

## Accounts without a Sandbox (`MANAGED_SA_RBAC`)

Static callers such as `claude-ai` get a Flux-owned `PolicyBinding` of the sets `CLAUDE_AI_SA`
approves, in place of their per-namespace RoleBindings in `agent_access_profiles.py`. Group
subjects (Haku OIDC; public-coder, until `PC_KUBERNETES_POLICY_SET`) remain Flux RoleBindings,
because a binding names a ServiceAccount only.

## Old snapshot grants (`OLD_POLICY_KINDS_DROP`)

Sandboxes created on agent-sandbox hold the old launch-time bindings and a `kubernetes-bindings`
finalizer, and the Sandbox Service no longer sees them: they are recreated on Agentplane's own
kind or cleaned by hand. The catalog fields, `kubernetes_grants.py` and the retained delegation
scopes (`kubernetes_binding_cleanup_namespaces`) leave with that drop.

## Verification owed on a deployed environment

Check with the affected accounts' own credentials, never the catalog as proof of access, and not
with a production account:

- Two Sandboxes sharing a set and a third with a one-off grant: add and remove a namespace in
  the set, widen and narrow a referenced Role, and revoke the one-off and the shared binding
  independently. State which accounts changed after each step.
- Delete a Sandbox, recreate a ServiceAccount of the same name, and restart the controller; an
  allowed and a denied request after each.
- A grant that fails partway: the failure shows in the binding's `KubernetesReady` condition and
  the rest still applies.
