# Policy Controller

Grants ServiceAccounts access by named, reusable `PolicySet`s, so a person chooses "basic cluster
diagnostics" rather than hundreds of per-namespace RoleBindings. It owns `PolicyBinding` expiry for
every rule type, and is the reader of the Kubernetes rule types of `PolicySet`
(`agentplane/crds/manifests/crd-policysets.yaml`).

## Expiry

The controller deletes a `PolicyBinding` once its `expiresAt` passes, so a binding grants by
existing, for every rule type: a reader of any rule type honours the bindings that exist and needs no
expiry handling of its own. A binding that a GitOps tool owns must not set `expiresAt`, or the tool
recreates it.

## Grants

- A `PolicyBinding` in the controller's policy namespace grants by existing: creating one allows,
  deleting one takes that back. It names one ServiceAccount, which must live in one of
  the controller's subject namespaces, and `PolicySet` names in its own namespace.
- A `kubernetes_role_binding` rule binds an existing Role or ClusterRole in each namespace it names;
  `kubernetes_cluster_role_binding` binds an existing ClusterRole cluster-wide. Namespaces are named,
  never selected. Rules never define permissions of their own.
- The API server enforces native RoleBindings and ClusterRoleBindings, which the controller keeps:
  one per binding, role and namespace, with the ServiceAccount as its only subject. Grants are
  additive and independent: revoking one binding never removes what another grants, and a binding
  whose other sets are missing or invalid still grants the rest.
- Every change to a set, a binding or a subject account converges the native bindings: missing ones
  are created, and those no existing binding grants are deleted, including after a rule, namespace or set is removed. So does a change to the native
  bindings themselves: one deleted or edited by hand is restored.
- A recreated ServiceAccount with a previous one's name gets new native bindings; those of the old
  account are deleted, since RBAC matches subjects by name alone.
- The controller claims a missing native binding name with create, then applies its managed labels,
  annotations, subjects and roleRef with server-side apply. Existing bindings are checked uncached;
  their UID and resourceVersion guard the apply against concurrent replacement or edits. It deletes
  stale grants explicitly, including old ServiceAccount UIDs, and replaces a managed binding if its
  immutable roleRef differs. An object at a desired name that does not carry this controller's
  ownership labels and binding identity is a collision; it is never adopted.
- The controller never creates a binding to a role that does not exist; it binds the role once it
  appears. It touches only native bindings carrying its label and its own policy namespace, so
  several Agentplane instances' controllers share a cluster.

## Revocation timing

Expiry takes effect when the controller deletes the `PolicyBinding`, and deletion when it deletes the
native bindings. While it is not running an expired binding stays and its native bindings keep
granting; nothing in the API server expires them.

## Status

Each `PolicyBinding` carries a `KubernetesReady` condition, applied under the controller's own field
manager so other readers' conditions stay. It is `Applied`, or `False` with reason
`SubjectNotWatched`, `SubjectMissing`, `RoleMissing`, `PolicySetUnavailable` or `ApplyFailed` and a
message naming what is wrong. Reasons are informational. With it the controller applies
`status.missingRoles`: each Role (with its namespace) or ClusterRole the binding's rules name that does
not exist. It is checked once the subject resolves and holds even when applying the rest fails
(`ApplyFailed`). The CRD schemas reject malformed sets and bindings.
