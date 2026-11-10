# Workload identity lifecycle

Status: **steps 1 to 4 landed, 2026-11-09; step 5 follows.** DAG node
[`WORKLOAD_AUTH_CUTOVER`](task_dag.md#workload_auth_cutover--migrate-workload-callers-and-remove-the-proxy-bearer-hop).
One issuer for owners' requests, hosted in the Policy
Controller, is implemented for Sandboxes: the workload CA (#15104, #15113, #15129, #15131), the
`WorkloadCredential` kind, requester and resolver (#15119), the issuer (#15127), the Sandbox
Controller as the first requester (c3771844), and the Action Service for command/build boxes
(#15192 and its template switch). The contract, and the constraints that rejected the
alternatives, are in [workload authentication](../docs/workload_authentication.md#workloadcredentials).
This plan covers moving the remaining callers onto it, so the proxy's `kubernetes_bearer` hop and
the workload bearer projections can be removed.

## Remaining callers

| Caller                     | Created by                     | Proves its incarnation today                                                | Policy subject today          | Its request                                                                                                          |
| -------------------------- | ------------------------------ | --------------------------------------------------------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Central sampler            | Flux Deployment                | Its own bearer; holds no SVID                                               | its own account               | out of scope; nothing acts as a central Session's identity yet (`CENTRAL_SESSION_ACTING_DESIGN` in the task freezer) |
| OpenClaw, external callers | `public_coder` cdk8s; none yet | None: its Deployment is at 0/0, and its dead relay config selects plaintext | `public-coder-agent/openclaw` | a standing workload once an instance exists; nothing before that                                                     |

**Per-incarnation credential IDs: revisit after the plan lands (operator, 2026-11-09).** The
cluster has no CRL or OCSP, so a fresh ID per accepted binding is what makes replacing an
incarnation revoke the old key, even one left in a terminating Pod, a VM disk or a Firecracker
snapshot. The alternative is one stable ID per record plus the Pod-binding check, which covers only
callers that forward a Pod-bound token. Revisit if the ID check proves redundant with binding checks
on every route, or if reissue latency hurts startup. Nothing in the migration waits on it.

## Authorization

**Coarse (operator, 2026-11-09).** RBAC grants `create`, `update` and `delete` on
`workloadcredentials` to exactly three accounts: the Sandbox Controller, the Action Service and
Flux's applier (all landed). Each may name any subject: a ServiceAccount in the record's namespace
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

## Migration and rollback

| Step                     | Mixed mode | Proven in k3s before staging                          | Rollback                        |
| ------------------------ | ---------- | ----------------------------------------------------- | ------------------------------- |
| 5. Remove the bearer hop | none       | Nothing projects the `agentplane-egress` hop audience | Revert; no caller depends on it |

Step 5 removes the `CLEANUP(added 2026-11-09)` set from #15065:

- the proxy's `forward.auth` bearer entry (`cluster/cdk8s/agentplane/egress.py`);
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
`SANDBOX_IDENTITY_SPEC` stops creating that account. A Sandbox caller's boxes already hang off the Sandbox, not the account;
with `SERVICES_IDENTITY_PRINCIPAL`, which gives the Action Service its caller as an Identity, a box
names that Identity as its subject and runs on the default ServiceAccount, and a caller with no
Sandbox has its records owned by its `Identity`. Its `kubectl` moves to impersonation with `EGRESS_IDENTITY_RULES`. Until the
cutover, requests name ServiceAccounts and services see unchanged subjects. The cutover PR switches
every requester's `subject` to an identity, and cdk8s renders `haku-ui` as an `Identity`. The
resolver already reads `spec.subject.identity` live with its UID, in the policy namespace rather than
the record's own, which matters for `haku-sandbox` records. Step 5
may land on either side.

## Open question

**Off-cluster workloads presenting SVIDs.** A Secret mount does not reach them. Recommendation:
design delivery when one exists. The record and the resolver stay as they are. The MCP edge path
for off-cluster agents is out of this design's scope: an OAuth Connection, forwarded to the Action
Service with the edge's backend credential. What changes at `IDENTITY_CUTOVER` is how the edge
binds a Connection to an Identity and proves it (the identity plan's MCP edge section).

When `WORKLOAD_AUTH_CUTOVER` lands, the rest of this content moves into
`docs/workload_authentication.md` and the SPECs of the Policy Controller, Sandbox Controller, egress
proxy and Action Service, and this file is deleted.
