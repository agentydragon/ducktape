# Policy Controller

Grants ServiceAccounts access by named, reusable sets, so a person chooses "basic cluster
diagnostics" rather than hundreds of per-namespace RoleBindings. It owns `PolicyBinding` expiry for
every rule type, and is the reader of the Kubernetes rule types of `PolicySet`
(`agentplane/crds/manifests/crd-policysets.yaml`), of `ServiceAccountGrant` and
`KubernetesRoleSet`, and of `ManagedServiceAccount`.

## Expiry

The controller deletes a `PolicyBinding`, `ServiceAccountGrant` or `ManagedServiceAccount` once
its `expiresAt` passes, and is the only deleter. The pass that deletes one also treats it as gone,
removing what it kept in the same pass. Until it is deleted, an expired object is absent to
readers too: the shared reader (`agentplane/policy/reader`) skips a `PolicyBinding` past its
`expiresAt` and tells its watchers at that instant, so a controller outage does not extend a
grant. Liveness is derived from `expiresAt` in that one reader; individual services never carry
expiry logic of their own. These three kinds are the only ones with an expiry. An object that a GitOps tool owns must not set `expiresAt`, or the tool recreates it.

An owner's clock is the only clock. A binding or grant that a `ManagedServiceAccount` owns goes with
the account and must not set its own `expiresAt`. Admission cannot see ownerReferences, so the
controller checks this instead. Such an object reports `KubernetesReady` `False` with reason
`ConflictingExpiry`, keeps no native bindings and is not expired until the conflict is removed.

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

`PolicyService.GrantSandboxPolicyBinding` and `RevokeSandboxPolicyBinding` are the operator write
boundary. Their Pod-bound writer allowlist is configured independently from the workload read/watch
resolver; an empty `writer_accounts` list denies both methods. Grant requires the exact live Sandbox
UID, its controller-owned ServiceAccount, and existing unique PolicySet names. It stores the Sandbox
UID in a non-controller owner reference. Revoke requires the caller's Sandbox UID and observed
PolicyBinding UID, refuses Flux and Sandbox-controller ownership, and deletes with both Kubernetes
UID and resourceVersion preconditions.

`PolicyService.DeleteIdentity`, behind the same writer allowlist, deletes one Identity by name and
observed UID with UID and resourceVersion preconditions; garbage collection then removes what the
Identity owns. It refuses an Identity carrying Flux's `kustomize.toolkit.fluxcd.io/name` label,
which Flux would recreate, and an Identity named by a PolicyBinding with no owner reference to that
Identity's UID: such a binding names a subject by name only, so it would grant a later Identity of
the same name. That refusal is `FAILED_PRECONDITION` with a `PreconditionFailure` violation per
binding. The check is advisory; a binding created between the read and the delete is not fenced.

Kubernetes does not transact a Sandbox read with a PolicyBinding create. Grant checks the exact
Sandbox and ServiceAccount before and after creation, and attempts UID/resourceVersion-guarded
cleanup if either identity changed. A replacement between those checks can still see a transient
name-based ServiceAccount grant before cleanup or owner-reference garbage collection; the owner UID
prevents that binding from belonging to a later Sandbox incarnation. Revoke's resourceVersion
precondition prevents deleting a binding edited or replaced after its read.

## ServiceAccount grants

- A `ServiceAccountGrant` in the policy namespace attaches `KubernetesRoleSet`s in its own
  namespace to one ServiceAccount, named by namespace and name. Its role sets'
  `role_binding` and `cluster_role_binding` rules mean what the `PolicySet` Kubernetes rule types
  mean, and the controller keeps their native bindings exactly as it keeps a binding's: one per
  grant, role and namespace; additive across grants and bindings on one account; converged on
  every change; never adopted on name alone; a recreated ServiceAccount bound afresh. A grant's
  native bindings carry its name and UID in place of a binding's, so a grant and a binding never
  share or prune each other's.
- A grant binds whatever ServiceAccount currently has its name; only `ManagedServiceAccount` refuses
  a foreign account of the same name.
- A role set naming a role the controller has no `bind` permission for reports `ApplyFailed`: the
  apiserver refuses the native binding.
- The ServiceAccount may live in a subject namespace or the accounts namespace, in the cluster the
  controller runs in.
- Its status is a binding's: `KubernetesReady` with reasons `Applied`, `SubjectNotWatched`,
  `SubjectMissing`, `RoleMissing`, `RoleSetUnavailable`, `ConflictingExpiry` or `ApplyFailed`, and
  `status.missingRoles`.

## Managed ServiceAccounts

- For a `ManagedServiceAccount`, the controller creates a ServiceAccount of the same name in the accounts namespace, labeled with the account's UID, and records that
  incarnation in `status.serviceAccount`. One deleted by hand is recreated.
- A ServiceAccount at that name without this account's UID label is never adopted: the reconcile
  fails, `status.serviceAccount` stays unset and `KubernetesReady` is `False` with reason
  `ServiceAccountConflict` until the name is free. One created for an earlier
  account of the same name is deleted and replaced.
- Once the account is gone, through expiry, its owner's deletion or by hand, the controller deletes
  the ServiceAccounts labeled with its UID and, in the same pass, every native binding on each of
  them, whichever binding or grant keeps it. No ownerReference reaches across namespaces, and no
  finalizer holds the account. Grants and bindings the account owns go by garbage collection.
- Its `KubernetesReady` condition is `Created`, `ServiceAccountConflict`, or `ConflictingExpiry`
  naming each binding or grant it owns that sets its own `expiresAt`.
- Deleting the ServiceAccount is not revocation by itself: RBAC matches by name, and impersonation
  needs no ServiceAccount object. The native bindings are the authority, which is why they go in
  the same pass; a grant whose ServiceAccount is missing keeps none (`SubjectMissing`).

## Workload credentials

With `workload_issuer` and `workload_namespaces` set, the controller hosts the
[workload-identity issuer](../workload_identity/README.md) for the `WorkloadCredential`s of those
namespaces. It is their status's only writer, and the only creator of Certificates for
three-segment workload URIs:

- A record gets a credential ID, the URI `spiffe://agentplane/<namespace>/<name>/<id>` and a
  Secret name in status before its Certificate exists.
- The first binding the controller sees for an ID becomes `status.acceptedBinding`. When
  `spec.binding` later differs from it, cleared or replaced, a fresh ID is minted; nothing else
  rotates an ID.
- A record with a controller owner or a binding gets one Secret per ID, `svid-<name>-<id>`. A
  standing record, with neither, keeps `svid-<name>`.
- `Ready` turns True once the Certificate for the current ID is Ready and its Secret holds the key
  pair. Superseded generations' Certificates are then deleted, and their Secrets once those
  Certificates are gone. A deleted record's Secrets go once the garbage collector removes its
  Certificates.

## Revocation timing

Expiry takes effect when the controller deletes the `PolicyBinding` or grant, and deletion when it
deletes the native bindings. While it is not running, the shared reader already treats an expired
binding as absent, but its native bindings keep granting: nothing in the API server expires them.

## Status

Each `PolicyBinding` carries a `KubernetesReady` condition, applied under the controller's own field
manager so other readers' conditions stay. It is `Applied`, or `False` with reason
`SubjectNotWatched`, `SubjectNotServiceAccount` (an identity subject, whose Kubernetes access is a grant instead), `SubjectMissing`, `RoleMissing`, `PolicySetUnavailable`, `ConflictingExpiry` or `ApplyFailed` and a
message naming what is wrong. Reasons are informational. With it the controller applies
`status.missingRoles`: each Role (with its namespace) or ClusterRole the binding's rules name that does
not exist. It is checked once the subject resolves and holds even when applying the rest fails
(`ApplyFailed`). The CRD schemas reject malformed sets and bindings.

## Policy resource watches

`PolicyService` serves the policy namespace on every controller replica. `WatchPolicyBindings`
observes bindings for one ServiceAccount, one Identity name, or every Identity subject;
`WatchPolicySets` observes PolicySets; `WatchIdentities` observes Identities; and
`ListPolicySetNames` returns one sorted snapshot of the PolicySet names visible to the caller. A
directly configured reader may select another subject or Identities, see every set and watch
Identities; other authenticated ServiceAccounts may read their own bindings and only sets named by
those bindings. Losing a binding
removes a set from that caller's view when no other binding names it. The server reviews the
PolicyService-audience bearer at admission, before writes, and while idle; a changed or refused
identity ends the stream.

Each stream sends snapshot chunks, `snapshot_complete`, then coalesced resource upserts and
UID-fenced removals. It opens Kubernetes watches before reading the snapshot, so an intervening
change is revisited. A closed or failed Kubernetes watch ends the RPC; a client must mark the data
stale and reconnect for a fresh snapshot. The watches do not provide an atomic cross-kind view.
No fixed timer forces healthy clients to reconnect.

The Kubernetes Go resource types and their validation markers remain authoritative. Explicit `proto`
field numbers on those types generate the resource protobuf messages at build time. Do not renumber
or reuse a field number. Kubernetes metadata carried by the public policy resource envelope is a
projection; policy specs and status come from the generated schema. PolicySet rules name credential
references; this API neither exposes credential values nor resolves EgressCredential metadata.
