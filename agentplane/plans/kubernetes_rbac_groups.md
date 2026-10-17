# Kubernetes grants through PolicySets: what remains

The model, its decisions and the rejected alternatives are in
[Policy sets](../docs/adr_policy_sets.md). The [Policy Controller](../policy_controller/SPEC.md)
runs on staging and `agentplane-testing`; the Sandbox Controller writes each Sandbox's
`PolicyBinding`; presets and the launch form name only `PolicySet`s; cdk8s renders every grant
catalog entry as a set. The [task DAG](task_dag.md) tracks the remaining nodes below.
[Agent RBAC](../../cluster/docs/agent_rbac.md) describes deployed scope.

## Old snapshot grants (`OLD_POLICY_KINDS_DROP`)

Sandboxes created on agent-sandbox hold the old launch-time bindings and a `kubernetes-bindings`
finalizer. The retired service path does not reconcile those legacy grants: they are recreated on
Agentplane's own kind or cleaned by hand. The catalog fields, `kubernetes_grants.py` and the retained
delegation scopes (`kubernetes_binding_cleanup_namespaces`) leave with that drop.

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
