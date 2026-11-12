# Sandbox Identity switch rollout

**2026-11-12 status (rollout owner):** #15742 and #15744 are merged and serving. The proposed
holds below were never applied. [Live evidence](../debug/2026_11_12_sandbox_identity_cutover/README.md)
records the observed sequence and remaining operator-approved broker acceptance. The Identity
caller UI repair #15860 is merged and awaiting frontend release. Treat the cutover steps below as
the prepared plan; do not repeat completed migration or inventory-retirement steps.

Prepared for rolling out the merged switch and its dependent cleanup. Remove this plan and its
companion hold patch after live acceptance and move any lasting operational instructions to the
owning service's documentation.

## Scope and merged contract

Every Sandbox names a live Identity; the controller creates no per-Sandbox ServiceAccount or
PolicyBinding. Launch callers create the Identity, grant the selected PolicySets, then create
the Sandbox. Presets contain their full grant list. The cleanup removes `spec.policySets`, the
Runner Gateway's default PolicySets and the legacy Sandbox credential fallback in the same paused
cutover. Platform components retain their ServiceAccounts for service authentication. The separate
standing-workload/MCP Identity cutover and environment-rule design remain separate work.
The source switch and cleanup are merged in [#15742](https://github.com/agentydragon/ducktape/pull/15742)
(`642063435fcd7246e3b973a45de103264bdeb867`) and
[#15744](https://github.com/agentydragon/ducktape/pull/15744)
(`004efb6317a3760fef668781e8d3fb3ba4cd63b9`). The evidence linked above records the deployed revisions and completed checks;
this prepared plan retains the remaining acceptance and rollback procedure.
Direct Identity callers and caller-delegated `sessions/create` cannot create Sessions in this cutover,
and Identities have no explicit-resume authority. Operator provisioning and the existing approved
`orchestration_v1` broker flow remain available. Caller-authorized child Sessions are tracked separately as
[`SESSION_SUBAGENT_DELEGATION`](task_dag.md#session_subagent_delegation--a-session-creates-and-messages-a-sub-session)
(operator reaffirmed 2026-11-12).

The operator completed the one-time migration from #15712; #15720 removed its script. Its namespace
was `agentplane-staging`. Do not rerun it. The rollout record reports an empty live Sandbox
inventory on 2026-11-12; recheck immediately before rollout. Existing sampler threads are archived,
and the operator accepts losing their live continuation state (2026-11-12); do not replay their
unresolved effects.
This does not authorize removing workspaces, other databases or work created after the cutover.

## Review and release gates

1. The source switch and cleanup have merged in #15742 and #15744. Before rollout, verify the
   candidate source, final-head checks, published images and rendered artifacts include the intended
   merged changes together. Use the read-only audit below before any hold or release action. Keep or
   establish the required holds until the coordinated release is ready; merged source alone does not
   authorize reopening launches.
2. Require green final-head CI, including controller startup under the deployed RBAC, app/API and
   orchestration launch and grant-admission tests, cdk8s generation drift, and the affected Pod/VM, MCP, egress,
   suspend/resume and Session-deletion system tests. Inspect visual artifacts for the changed app
   launch flow. A timeout needs diagnosis; it is not a reason to widen it.
3. Verify rollback is possible before reopening launches. Record exact source/artifact revisions,
   serving image tags/digests, current Identities/PolicyBindings/WorkloadCredentials, Sandbox UIDs,
   retained workspace claims and relevant controller conditions. Store metadata without Secret
   data. Preserve the snapshots outside disposable Sandboxes.

## Read-only state audit before action

Start with observation only. Before creating holds, changing pins, pausing launches, or applying
acceptance steps, record the current source revision and inspect the live owners and workloads:

- For `agentplane-crds`, `agentplane-staging` and `agentplane-staging-delivery`, read their live
  `sourceRef`, observed artifact digest/revision, readiness, and `spec.suspend`. Inspect the shared
  `all-images` automation's state and its previous value before considering any hold.
- Compare the current image pins with each Deployment's observed generation and actual image IDs.
  Record which, if any, are demonstrably built from #15742 and #15744. A Git revision, tag suffix,
  or successful reconciliation alone does not establish which binary is running.
- Inventory Sandboxes, their UIDs and controller conditions, active runner incarnations, and the
  related Identities, PolicyBindings and WorkloadCredentials. Preserve retained workspace claims
  and other user-authored data. Record metadata only; do not collect Secret contents.
- Record any existing Flux or image-automation holds and their owner. Do not replace, remove, or
  recreate a hold during this audit.

The source merges may already have reconciled. If the switch is deployed, do not repeat the cutover
or one-time migration. Record the deployment evidence, retain applicable existing holds, and run
only acceptance or release checks that remain outstanding. If the switch is not deployed, proceed
with the hold and rollout steps below, adapted to the state observed. Do not infer a live state from
this plan or its dated inventory snapshot.

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

If the read-only audit shows the switch is not yet deployed, establish a temporary, declarative hold
for these three Kustomizations in their owning generator and reconcile that hold before rollout.
Verify the live `spec.suspend` values. Keep the hold in Git until the coordinated release is ready;
an imperative suspend alone can be overwritten by the parent reconciler. If image automation must
also be held, coordinate the shared `all-images` automation with its other consumers and record its
previous state. If the switch is already deployed, preserve and verify existing holds; do not add
or remove them until the operator has assessed the recorded state. Preparation of this plan does
not apply any hold or change running workloads.

The companion [`sandbox_identity_rollout_hold.patch`](sandbox_identity_rollout_hold.patch) is a
rendered candidate for that temporary hold. It changes the three owning cdk8s functions and only
these three generated Kustomizations. The delivery function currently has a single caller, for
staging. Rebase the candidate into a separate hold PR on the current deployment branch, regenerate
with `bb run //cluster/cdk8s:generate_manifests`, and review its exact scope before merging. Its
earlier generation and `git apply --check` passed against the repaired switch; regenerate against
the current branch before use. The cutover itself leaves reconciliation enabled; merely checking
out or merging this patch artifact applies no hold.

## Cutover

1. For an undeployed switch, pause every Sandbox and prevent app/Action launches during the
   maintenance window. Verify every existing Sandbox reports stopped and no old runner incarnation
   remains; if inventory is empty, record the fresh audit result. Retained History and workspace
   data stay intact. If the audit shows the switch is already deployed, do not repeat this cutover
   step; preserve the observed state and use the remaining acceptance and release checks.
2. For an undeployed switch with the holds verified, roll out the merged source. Wait for the
   intended images and rendered artifacts to publish. Pin the candidate images together and verify
   the rendered artifacts contain those pins, the Identity-only controller RBAC/watch set, VM
   launcher policy and cleanup CRD/settings. A reused unchanged image needs content/revision
   evidence; a matching tag suffix on only one service is insufficient. If already deployed, record
   the exact applied artifact and image evidence from the read-only audit instead of redeploying.
3. For a rollout still in progress, remove the CRD hold, reconcile and verify its schema. Remove the
   main staging hold with the complete candidate image/configuration set, then the delivery hold
   once its dependency is ready. Keep launches paused through any temporary mixed-version interval.
   Record each applied artifact digest and each Deployment's observed generation and actual image
   IDs. If these changes are already applied, leave holds as found and record the observed sequence;
   do not repeat completed release steps.
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
