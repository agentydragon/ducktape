# Workload identity lifecycle

Status: **steps 1 and 2 landed; steps 3 and 4 in flight, 2026-11-09; step 5 follows.** DAG node
[`WORKLOAD_AUTH_CUTOVER`](task_dag.md#workload_auth_cutover--migrate-workload-callers-and-remove-the-proxy-bearer-hop).
One issuer for owners' requests, hosted in the Policy
Controller, is implemented for Sandboxes: the workload CA (#15104, #15113, #15129, #15131), the
`WorkloadCredential` kind, requester and resolver (#15119), the issuer (#15127) and the Sandbox
Controller as the first requester (c3771844). The contract, and the constraints that rejected the
alternatives, are in [workload authentication](../docs/workload_authentication.md#workloadcredentials).
This plan covers moving the remaining callers onto it, so the proxy's `kubernetes_bearer` hop and
the workload bearer projections can be removed.

## Remaining callers

| Caller                     | Created by                                                             | Proves its incarnation today                                                | Policy subject today          | Its request                                                                                                          |
| -------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Action command/build box   | Action Service: a bare Pod from a `PodTemplate`, owned by its record   | Pod-bound `agentplane-egress` bearer, TokenReview                           | the calling Sandbox's own SA  | its `WorkloadCredential`, requested by the Action Service; the sidecar does not present it yet                       |
| Haku UI                    | Haku: a StatefulSet in `haku-state`, applied to `haku-sandbox` by Flux | Pod-bound bearer of SA `haku-ui`                                            | `haku-sandbox/haku-ui`        | a `WorkloadCredential` rendered by cdk8s, named after the StatefulSet, with no owner and no binding                  |
| Anki                       | as Haku UI, a Deployment `haku-anki`                                   | Pod-bound bearer of SA `haku-ui`                                            | `haku-sandbox/haku-ui`        | its own record `haku-anki`, same subject ([below](#standing-workloads))                                              |
| Central sampler            | Flux Deployment                                                        | Its own bearer; holds no SVID                                               | its own account               | out of scope; nothing acts as a central Session's identity yet (`CENTRAL_SESSION_ACTING_DESIGN` in the task freezer) |
| OpenClaw, external callers | `public_coder` cdk8s; none yet                                         | None: its Deployment is at 0/0, and its dead relay config selects plaintext | `public-coder-agent/openclaw` | a standing workload once an instance exists; nothing before that                                                     |

## The remaining records

```yaml
metadata:
  name: agent-123-scratch # the box Pod's name; the Pod's controller owner is this record
  namespace: agentplane-staging
  # the caller account's controller, else the account
  ownerReferences: [{ kind: Sandbox, name: agent-123, uid: <sandbox-uid>, controller: true }]
spec: # the caller
  subject: { serviceAccount: agent-123 }
  binding: { podUID: <pod-uid> }
  pod: { name: agent-123-scratch, uid: <pod-uid> }
---
metadata: { name: haku-ui, namespace: haku-sandbox } # rendered by cdk8s; no owner
spec: { subject: { serviceAccount: haku-ui } } # no binding
status: { secretName: svid-haku-ui } # stable: a Flux-applied Deployment must name it
```

**Per-incarnation credential IDs: revisit after the plan lands (operator, 2026-11-09).** The
cluster has no CRL or OCSP, so a fresh ID per accepted binding is what makes replacing an
incarnation revoke the old key, even one left in a terminating Pod, a VM disk or a Firecracker
snapshot. The alternative is one stable ID per record plus the Pod-binding check, which covers only
callers that forward a Pod-bound token. Revisit if the ID check proves redundant with binding checks
on every route, or if reissue latency hurts startup. Nothing in the migration waits on it.

## Authorization

**Coarse (operator, 2026-11-09).** RBAC grants `create`, `update` and `delete` on
`workloadcredentials` to exactly three accounts: the Sandbox Controller (landed), the Action
Service and Flux's applier. Each may name any subject: a ServiceAccount in the record's namespace
until the cutover, and after it any `Identity` in the policy namespace. So each of those accounts
can make a workload act as any identity in its namespace. The real gates stay upstream:

- for a Sandbox, who may set `Sandbox.spec.identity`;
- for a box, the caller the Action Service authenticated;
- for a standing workload, git.

Flux's kustomize-controller already holds `*`, so a narrower grant to Flux would record intent but
not enforce it. Haku cannot author a record, because the `haku-state` Kustomization impersonates
`haku-state-reconciler`, which holds no right on `WorkloadCredential`s. A narrower requester can be
added later without changing anything here: a `ValidatingAdmissionPolicy` calling the CEL
`authorizer` for a custom `use` verb on the named identity.

The requesters' grant that remains:

```yaml
kind: Role # agentplane-staging; Flux's applier gets the same in haku-sandbox
rules:
  - {
      apiGroups: [agentplane.allegedly.works],
      resources: [workloadcredentials],
      verbs: [get, list, watch, create, update, delete],
    }
# bound to the Action Service
```

## Standing workloads

Haku UI (a StatefulSet) and Anki (a Deployment) are Haku-authored workloads with replicas, not
controller-owned incarnations. cdk8s renders one record per workload into `haku-sandbox`
(`cluster/cdk8s/haku/ui_egress.py`), with no owner and no binding. Haku's deployer Role cannot write
it. The issuer gives it a stable Secret name, `svid-<name>`, and Haku's manifest mounts that Secret
into its egress sidecar alone, in place of the token projection. Any Pod Haku creates there could
mount it, which is the reach the `haku-ui` ServiceAccount already gives. The incarnation is the
record itself. Every replica presents the same SVID, and no Pod is proven, so the proxy drops any
Pod token such a caller forwards ([egress SPEC](../egress/SPEC.md)). Neither sidecar forwards one:
the hop projection is their only token. Revocation is deleting the record, which Flux recreates with
a fresh ID, or a Flux prune. A running sidecar refuses a reloaded SVID with a different URI
(`environment_runtime_transport` pins the first), so after a recreate the workload regains egress
only on a rollout restart.

Neither workload may lose egress, so step 4 lands in this order:

1. The proxy accepts standing SVIDs (#15201).
2. cdk8s renders the records and adds `haku-sandbox` to the issuer's namespaces and the proxy's
   record grant; it may land in parallel with 1, since the sidecars still use the bearer.
3. Once both have rolled out and both records are `Ready`, a `haku-state` commit switches each
   sidecar to `auth.mode: spiffe` with `svid-<name>` mounted, and drops the hop projection.

Decided for now (2026-11-09): no Pod provenance for standing replicas, since decision history and
LLM ingress metadata name the record and its credential ID; and Anki has its own record with subject
`haku-ui`, to be split into its own identity when its grants diverge. One shared non-rotating
credential with no Pod provenance is weaker than the per-incarnation credentials of Sandboxes and
boxes. To revisit: bring standing workloads up to per-incarnation credentials, or accept the weaker
guarantee everywhere.

## Migration and rollback

| Step                                                            | Mixed mode                | Proven in k3s before staging                                                                            | Rollback                                             |
| --------------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 3. Action boxes request records; the box sidecar selects SPIFFE | Proxy still allows bearer | Box egress and rules route by SVID; dispose revokes on the next request; caller A's box cannot act as B | Revert the Action Service image and templates        |
| 4. Standing records; `haku-state` switches its sidecars         | as above                  | A generic standing Deployment with two replicas authenticates, and its prune revokes                    | Revert the `haku-state` commit; the request is inert |
| 5. Remove the bearer hop                                        | none                      | Nothing projects the `agentplane-egress` hop audience                                                   | Revert; no caller depends on it                      |

Step 5 removes the `CLEANUP(added 2026-11-09)` set from #15065:

- the proxy's `forward.auth` bearer entry (`cluster/cdk8s/agentplane/egress.py`);
- the command-box hop projection (`command_sandbox.py`);
- the sidecar's `KubernetesBearerProxyAuth` and plaintext transport (`egress/sidecar_config.py`,
  `egress/sidecar/config.go`);
- the forward listener's bearer mode and hop verifier (`egress/identity/authentication.go`).

It also deletes or converts `add_relay_settings` in `cluster/cdk8s/public_coder/egress.py`, which renders both removed
models. That config is dead: the `public-coder-agent` Deployment is scaled to 0/0, and #15089
tombstones it. `kubernetes_bearer` stays on service listeners for backend callers.

**With `IDENTITY_CUTOVER`; the ordering edge and the owner switch were decided by the operator on
2026-11-09.** Box creation needs the caller's own ServiceAccount. `stamp` in
`action_service/sandbox/inventory.go` reads it, runs the box as it, names it as the record's
subject, and makes its controller, the calling Sandbox, the record's owner, which owns the box.
`SANDBOX_IDENTITY_SPEC` stops creating that account, so step 3 is a technical prerequisite of that
node, recorded in the DAG. A Sandbox caller's boxes already hang off the Sandbox, not the account;
in the same change, a caller with no Sandbox has its records owned by its `Identity`. Its `kubectl` moves to impersonation with `EGRESS_IDENTITY_RULES`. Until the
cutover, requests name ServiceAccounts and services see unchanged subjects. The cutover PR switches
every requester's `subject` to an identity, and cdk8s renders `haku-ui` as an `Identity`. The
resolver then reads `spec.subject.identity` through the shared policy reader, live with its UID, in
the policy namespace rather than the record's own, which matters for `haku-sandbox` records. Steps 4
and 5 may land on either side.

## Open question

**Off-cluster workloads presenting SVIDs.** A Secret mount does not reach them. Recommendation:
design delivery when one exists. The record and the resolver stay as they are. The MCP edge path
for off-cluster agents is out of this design's scope: an OAuth Connection, forwarded to the Action
Service with the edge's backend credential. What changes at `IDENTITY_CUTOVER` is how the edge
binds a Connection to an Identity and proves it (the identity plan's MCP edge section).

When `WORKLOAD_AUTH_CUTOVER` lands, the rest of this content moves into
`docs/workload_authentication.md` and the SPECs of the Policy Controller, Sandbox Controller, egress
proxy and Action Service, and this file is deleted.
