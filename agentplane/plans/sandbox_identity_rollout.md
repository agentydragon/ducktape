# Sandbox Identity switch rollout

**2026-11-12 status:** #15742 and #15744 are merged and serving. The proposed holds below were never applied.
[Actual rollout and live evidence](../debug/2026_11_12_sandbox_identity_cutover/README.md) record the observed sequence and remaining
operator approval UI defect. Treat the cutover steps below as the prepared plan, not instructions to repeat the migration.

Prepared for the switch and its dependent cleanup. Remove this plan and its companion hold patch after live acceptance and
move any lasting operational instructions to the owning service's documentation.

## Scope and accepted state

Every Sandbox names a live Identity; the controller creates no per-Sandbox ServiceAccount or
PolicyBinding. Launch callers create the Identity, grant the selected PolicySets, then create
the Sandbox. Presets contain their full grant list. The cleanup removes `spec.policySets`, the
Runner Gateway's default PolicySets and the legacy Sandbox credential fallback in the same paused
cutover. Platform components retain their ServiceAccounts for service authentication. The separate
standing-workload/MCP Identity cutover and environment-rule design remain separate work.
Direct Identity callers and caller-delegated `sessions/create` cannot create Sessions in this cutover,
and Identities have no explicit-resume authority. Operator provisioning and the existing approved
`orchestration_v1` broker flow remain available. Caller-authorized child Sessions are tracked separately as
[`SESSION_SUBAGENT_DELEGATION`](task_dag.md#session_subagent_delegation--a-session-creates-and-messages-a-sub-session)
(operator reaffirmed 2026-11-12).

The operator completed the one-time migration from #15712; #15720 removed its script. Its namespace
was `agentplane-staging`. Do not rerun it. On 2026-11-12 the live Sandbox inventory was empty.
Recheck immediately before the cutover. Existing sampler threads are archived and the operator
accepts losing their live continuation state (2026-11-12); do not replay their unresolved effects.
This does not authorize removing workspaces, other databases or work created after the cutover.

## Review and release gates

1. Review the switch and cleanup as separate incremental diffs, with the cleanup based on the
   switch. Keep both draft until the maintenance window is agreed. Do not merge just the cleanup
   or let image automation deploy a partially reviewed switch.
2. Require green final-head CI, including controller startup under the deployed RBAC, app/API and
   orchestration launch and grant-admission tests, cdk8s generation drift, and the affected Pod/VM, MCP, egress,
   suspend/resume and Session-deletion system tests. Inspect visual artifacts for the changed app
   launch flow. A timeout needs diagnosis; it is not a reason to widen it.
3. Verify rollback is possible before reopening launches. Record exact source/artifact revisions,
   serving image tags/digests, current Identities/PolicyBindings/WorkloadCredentials, Sandbox UIDs,
   retained workspace claims and relevant controller conditions. Store metadata without Secret
   data. Preserve the snapshots outside disposable Sandboxes.

## Owners and holds

The application namespace is `agentplane-staging`. The Flux Kustomizations are in
`ducktape-flux`, not `flux-system`:

| Owner                         | Applies                                                                        |
| ----------------------------- | ------------------------------------------------------------------------------ |
| `agentplane-crds`             | Sandbox CRD and the other Agentplane CRDs                                      |
| `agentplane-staging`          | Main environment, policy/RBAC and most service workloads                       |
| `agentplane-staging-delivery` | Separate Environment Gateway serving artifact; depends on `agentplane-staging` |

They consume `ExternalArtifact` sources. Read each live `sourceRef` and observed artifact digest;
the Git revision alone does not prove which rendered artifact was applied. Image pins are in
`cluster/k8s/agentplane-staging/image-pins/kustomization.yaml`. The `flux-system/all-images`
ImageUpdateAutomation updates that path as part of a shared repository-wide automation.

Before merging the cutover, make a temporary, declarative hold for these three Kustomizations in
their owning generator and reconcile that hold. Verify the live `spec.suspend` values. Keep the
hold in Git until the coordinated release is ready; an imperative suspend alone can be overwritten
by the parent reconciler. If image automation must also be held, coordinate the shared
`all-images` automation with its other consumers and record its previous state. Preparation of
this plan does not apply any hold or change running workloads.

The companion [`sandbox_identity_rollout_hold.patch`](sandbox_identity_rollout_hold.patch) is a
rendered candidate for that temporary hold. It changes the three owning cdk8s functions and only
these three generated Kustomizations. The delivery function currently has a single caller, for
staging. Apply the patch in a separate hold PR based on the current deployment branch, regenerate
with `bb run //cluster/cdk8s:generate_manifests`, and review its exact scope before merging. Its
generation and `git apply --check` passed against the repaired switch. The cutover stack itself
leaves reconciliation enabled; merely checking out or merging this patch artifact applies no hold.

## Cutover

1. Pause every Sandbox and prevent app/Action launches during the maintenance window. Verify
   every existing Sandbox reports stopped and no old runner incarnation remains; if inventory is
   still empty, record that fact. Retained History and workspace data stay intact.
2. With the holds verified, merge the reviewed stack. Wait for the changed images and rendered
   artifacts to publish. Pin the intended candidate images together and verify the rendered
   artifacts contain those pins, the Identity-only controller RBAC/watch set, VM launcher policy
   and the cleanup CRD/settings. A reused unchanged image needs content/revision evidence; a
   matching tag suffix on only one service is insufficient.
3. Remove the CRD hold, reconcile and verify its schema. Remove the main staging hold with the
   complete candidate image/configuration set, then the delivery hold once its dependency is
   ready. Keep launches paused through any temporary mixed-version interval. Record each applied
   artifact digest and each Deployment's observed generation and actual image IDs.
4. Check all required Deployments and Flux owners are Ready. In particular, the Sandbox Controller
   must finish cache synchronization without ServiceAccount/PolicyBinding watch errors. Check the
   new settings against the serving binaries, and verify the Kyverno launcher policy on the VM
   path. These cluster admission/Flux resources reach staging without a full operator-system test;
   record the first live result explicitly.

## Acceptance before reopening launches

- Launch a disposable Sandbox from the app and through the supported operator-approved
  `orchestration_v1` flow. Confirm separate Identity creation, exactly selected grants, and a
  Sandbox whose `spec.identity` names that Identity. No per-Sandbox ServiceAccount or controller
  grant is created. A preset-less launch receives only its explicit selections.
- Confirm its Pod/VM launcher uses `default` with token automount disabled, and its Sandbox-owned
  WorkloadCredential names the live Identity UID. A missing Identity starts nothing; stale
  credential subjects are refused/replaced, never retargeted on an active incarnation.
- From inside the launched Sandbox, discover the egress Kubernetes account and perform a permitted
  diagnostics read. Verify the intended shared account/authorization, and verify an operation
  outside its grant is denied. Host-admin `kubectl` is not evidence for this path.
- Have the operator create a fresh Session, finish a tool-using turn and submit its next input.
  Verify its Identity's direct and caller-delegated `sessions/create` attempts are denied and create no
  child, while it can steer an operator-provisioned same-Identity child. Exercise operator
  suspend/resume and the changed VM launcher path. Archived sampler continuations need not resume.
- Refuse a changed same-name launch and confirm the existing Identity, grants and Sandbox survive.
  A definitively refused new launch may roll back only the Identity that attempt created; an
  uncertain creation outcome remains for reconciliation.
- Delete only the acceptance Sandbox and verify credential cleanup. Its separate Identity remains
  until explicitly deleted under the existing reference guard. Preserve any non-test workspace.

Record the candidate source, CI links, merged commits, applied artifact digests, serving image IDs
and live test results separately. Reopen launches and remove the remaining temporary holds only
after these checks pass. Restore shared automation to its recorded state.

## Rollback

Before reopening launches, stop acceptance workloads and preserve their diagnostic artifacts.
Revert the switch and cleanup configuration/schema changes together, restore the recorded
compatible image pins, and publish/reconcile the resulting artifacts under the same holds. Restore
the old Role/watch/configuration set together; reverting only the controller image with its new
restricted Role will fail cache synchronization. Verify Deployment generations, image IDs and Flux
readiness before lifting the holds. No database or workspace deletion is part of this rollback.

After reopening launches, first stop new work and inventory what the new version created. The
operator's permission to lose pre-cutover sampler threads does not cover these new Sessions or
their workspaces. Preserve that state and choose a forward repair or an explicitly reviewed
rollback accounting for the new Identity-bound objects; do not restore an older snapshot blindly.
