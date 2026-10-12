# Kubernetes Policy Controller

Grants ServiceAccounts Kubernetes access by named, reusable `PolicySet`s, so a person chooses
"basic cluster diagnostics" rather than hundreds of per-namespace RoleBindings. It is the reader of
the Kubernetes rule types of `PolicySet` (`agentplane/crds/manifests/crd-policysets.yaml`).

## Grants

- A `PolicyBinding` in the controller's policy namespace grants by existing and unexpired: creating
  one allows, deleting one takes that back. It names one ServiceAccount, which must live in one of
  the controller's subject namespaces, and `PolicySet` names in its own namespace.
- A `kubernetes_role_binding` rule binds an existing Role or ClusterRole in each namespace it names;
  `kubernetes_cluster_role_binding` binds an existing ClusterRole cluster-wide. Namespaces are named,
  never selected. Rules never define permissions of their own.
- The API server enforces native RoleBindings and ClusterRoleBindings, which the controller keeps:
  one per binding, role and namespace, with the ServiceAccount as its only subject. Grants are
  additive and independent: revoking one binding never removes what another grants, and a binding
  whose other sets are missing or invalid still grants the rest.
- Every change to a set, a binding or a subject account, and every binding's `expiresAt`, converges
  the native bindings: missing ones are created, and those no live, unexpired binding grants are
  deleted, including after a rule, namespace or set is removed. Native bindings changed by hand are
  repaired within one resync period.
- A recreated ServiceAccount with a previous one's name gets new native bindings; those of the old
  account are deleted, since RBAC matches subjects by name alone.
- The controller never creates a binding to a role that does not exist, and touches only native
  bindings carrying its label and its own policy namespace, so several Agentplane instances' controllers
  share a cluster.

## Revocation timing

Deletion and expiry take effect when the controller deletes the native bindings. While it is not
running they keep granting; nothing in the API server expires them.

## Status

Each `PolicySet` and `PolicyBinding` carries a `KubernetesReady` condition, applied under the
controller's own field manager so other readers' conditions stay. A set is `Valid` or `Invalid`. A
binding is `Applied`, or `False` with reason `Invalid`, `Expired`, `SubjectNotWatched`,
`SubjectMissing`, `PolicySetUnavailable` or `ApplyFailed` and a message naming what is wrong.
