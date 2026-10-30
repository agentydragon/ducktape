# Agentplane Kubernetes ownership preflight

2026-10-30. Read-only source review; no live mutations, no fault injection, no implementation changes.
Reviewed source HEAD 09e2be2e977319c89941544a5f7b59f8039dc876 against fetched origin/devel b92ba5c446e13efe8aeb86544066c13f5d98c6a7. The finding paths other than the already-discussed controller Lease interpretation have no intervening diff. Pending PR #13040 was inspected separately; its worktree is clean and pushed.

## Ownership inventory

- Sandbox Controller writes Sandbox status and owns its Pod/VM, identity ServiceAccount, launch ConfigMap, control Service, PolicyBinding, PVCs and runner Certificates.
- KubeVirt owns VMI and launcher Pod; controller observes those descendants. VM start updates managed template/spec and metadata; stop updates run strategy.
- Runner Gateway owns observation Lease and control EndpointSlice writes. Their non-controller Sandbox owner references provide garbage collection, not proof of connection.
- Dispatcher owns separate ownership Leases and DB ownership generations.
- cert-manager produces runner TLS Secrets; controller checks declared metadata and cleans them up because certificate owner references are not guaranteed.
- Action launch executor creates non-controller Sandbox-owned launcher PolicySets/PolicyBindings.
- Policy Controller writes native RBAC bindings using managed labels plus binding/subject UIDs; cross-namespace and cluster-scoped objects use explicit cleanup, not illegal cross-scope owner references.
- Policy readiness readers share PolicyBinding status through separate condition types and SSA field managers. Conditions are a map keyed by type. This is an intentional existing multiwriter contract.
- App policy UI creates/revokes operator bindings and infers Git/Sandbox/operator provenance.

## Findings

### A — Launcher grants are misclassified as operator-owned

`agentplane/action_service/orchestration_v1/launch.go:164` creates Sandbox owner references without controller=true. `agentplane/app/go/policyaccess/views.go:368` recognizes only controller Sandbox references. `policyaccess.go:215` therefore permits revoking a launcher-owned binding as if operator-created.

Fix: identify Sandbox lifecycle ownership independently of the controller flag, verify API group/kind and identity, and refuse revocation of any resource owned by another component. Test actual launch-grant metadata through provenance and revoke, plus unknown owners.

### B — Checked objects can be replaced before unconditional deletion

`agentplane/action_service/sandbox/inventory.go:318` checks caller labels, then deletes by name with no UID precondition. `agentplane/app/go/policyaccess/policyaccess.go:215` likewise checks provenance and deletes without UID/resourceVersion guards. A same-name replacement between check and delete is not covered by the ownership decision. A same-UID provenance change also matters for operator revocation.

Fix: UID-bound deletes; use resourceVersion where the authorization depends on mutable labels/provenance. Tests replace or relabel the object between read and delete and require refusal. Existing `app/go/sandboxes/inventory.go:315` demonstrates UID+resourceVersion deletion.

### C — Controller reads connection Leases without checking Sandbox ownership

`agentplane/sandbox_controller/controller.go:320` retrieves the expected Lease name and returns annotations without checking its Sandbox owner UID, API or deletion state. Gateway writers reject foreign Leases, but controller lifecycle readers do not. A collision can affect bootstrap, suspend holds, quiescence and verified-contact decisions even while Gateway refuses to adopt it.

Fix: validate ownership at the common runnerLease read boundary; expose a conflict rather than interpret foreign annotations. Validate pending/expired/verified state separately. Test previous Sandbox UID, foreign API/kind, missing owner and deletion. PR #13040 repairs claim/contact interpretation but does not add this owner check.

### D — VMI discovery trusts owner name/kind without VM identity

`agentplane/sandbox_controller/vm.go:36` accepts a VMI whose controller owner is named like the Sandbox VM, without verifying API version, VM UID or the VM-to-Sandbox chain. It can publish an unrelated/stale launcher's incarnation and wait on an unrelated VMI during teardown. Gateway's `gateway/peer.go` performs a substantially stronger chain check, so the components can disagree.

Fix: validate the full chain when the VM exists. Define the orphan/deleting-VM case explicitly using recorded identity; do not recover identity by name alone. Test same-name foreign VM, stale VM UID, wrong API, and VM deletion before VMI disappearance.

### E — Policy status applies are not bound to the observed resource identity

`agentplane/policy/client/apply/conditions.go:26` builds status apply by namespace/name without UID or resourceVersion. Both Policy Controller and readiness readers use it. If a binding is replaced between read and apply, an old observation can land on the replacement; generation alone does not disambiguate recreated resources.

Fix: include the observed UID and choose optimistic version checking where required; on conflict, recompute from current state. Keep per-condition SSA ownership. Test replacement between cache read and status apply with the same name and generation.

### F — Dispatcher ownership validation is weaker than Gateway's

`agentplane/runner_gateway/dispatcher/ownership.go:45` checks only owner kind and UID, while Gateway's `gateway/records.go:247` checks API version, kind, name and UID. Dispatcher acquisition also does not reject deletionTimestamp before attempting ownership. Kubernetes UID uniqueness reduces accidental aliasing but does not make malformed owner references a valid ownership contract.

Fix: use explicit complete owner validation and reject terminating coordination objects; retain DB-generation fencing. Test wrong API/name, deleting Lease, and recreated Lease UID.

## Preflights that look sound

- applyOwned uses exclusive create, uncached collision checks, UID+resourceVersion-bound SSA, and checks deletion before force-applying managed fields.
- PVCs record immutable UID/layout before workloads start; same-name foreign/replaced claims are blocked. Stop retains storage.
- Main workload deletes use UID preconditions.
- Gateway observation writes and EndpointSlice adoption check full owner identity and optimistic resource versions.
- Policy Controller native RBAC apply uses exclusive create, explicit management identity and guarded SSA; deletes normally use observed UIDs.
- Readiness status conditions have distinct field managers and keyed-list schema; shared status does not by itself require a new CRD.

## Additional design concerns to retain

- Secret cleanup uses labels/name/certificate-name evidence rather than mandatory owner references. This is intentional for current cert-manager behavior, but warrants a foreign-owner collision regression; labels are a management contract, not authentication.
- Sandbox Controller RBAC grants create/update on Leases although its reviewed production code reads them. Narrow to read-only if no other binary path needs writes.
- History Service caches Sandbox ownership by ServiceAccount UID after fetching the current account. An owner-reference mutation on the same UID does not invalidate that cache. Decide whether owner immutability is enforced; otherwise derive from the freshly fetched account or invalidate on ownership changes.
- applyOwned's exception for Gateway-created non-controller-owned Services appears historical: current Gateway creates EndpointSlices, not Services. Remove only after checking deployed ownership metadata; do not silently adopt arbitrary Services on matching names.
- A reconnect claim resets runtime facts. Durable verified contact added by #13040 must survive claims, while current liveness must still require fresh WatchState evidence.

## Suggested execution

Start with A+B as independent focused PRs; C is the closest follow-up to this incident. D and E each need concurrency/identity regressions. F and RBAC narrowing are smaller consistency follow-ups. No resource split is needed to fix these findings.

These are source-grounded defects or review concerns, not claims of observed exploitation. No tests were added or executed for this audit.
