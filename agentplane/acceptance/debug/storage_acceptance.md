# Fresh testing storage acceptance

The manual `test_storage_deployed` target uses only uniquely named fixture SandboxTemplates and
Sandboxes in `agentplane-testing`. It verifies the exact accepting controller pin before writing
layout declarations. No deployed template, existing Sandbox, staging resource, guest formatting
implementation or conversion path is changed.

| Fixture          | State                            | Workspace                                                    | Expected after stop/start                                                         |
| ---------------- | -------------------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------------------- |
| Pod              | Fixed DirectoryTree              | Explicit DirectoryTree at `/workspace`                       | Both PVC identities and exact file bytes retained                                 |
| VM selected path | Fixed DirectoryTree via virtiofs | Explicit RawDiskImage/ext4 on a Sandbox-owned filesystem PVC | Both PVC identities and exact file bytes retained                                 |
| VM comparison    | Fixed DirectoryTree via virtiofs | Existing VM-owned blank DataVolume                           | Both retained on halt/start; state retained and workspace empty on VM replacement |

Bootstrap writes unique synthetic markers once. A Session setup script checks their exact contents
before and after workload replacement, records checksums and the actual workspace mount in canonical
history, and sends no model turn. The test also checks ownership, creation-time format record and
typed status binding for the retained workspace. Before resuming each retained workspace, a fixture claim-size change must produce
`VolumeLayoutMismatch` without starting a workload or replacing the PVC; restoring its original
declaration then permits resume. The test waits on controller conditions and resource
deletion events, then deletes only fixture UIDs with Kubernetes deletion preconditions. One case
runs at a time and each begins with no other testing Sandboxes and 5Gi of the 80Gi quota used.

Initial observed testing pin: controller `devel-20261014073117-025621f`, Ready; its source contains
the #10440 workspace record/layout implementation. Pod runner `66a1b03`; VM image `9642b3d`, with
runner wheel release `a13d9a7d58ff` resolving to `619da1cd53015ffcfd97a7bdfb561e53111abbaf` as verified
in the earlier Codex journal acceptance. The existing VM journal/native-context pass is reused;
this matrix does not ask a model to repeat it.

All three testing rows passed as recorded below. Sandbox-owned `RawDiskImage` is the selected VM
path because it retains the workspace through VM-object replacement; the old DataVolume does not.
The Pod workspace remains `DirectoryTree`. This proves
fresh provisioning and replacement only: no existing-data cutoff, kind conversion, BlockFilesystem,
controller restart, node portability or staging acceptance is claimed. Rollout and cutoff remain
separate gates in the task DAG.

Before a run, verify the controller image's source accepts #10440, then supply that exact full
reference as `AGENTPLANE_STORAGE_CONTROLLER_IMAGE`. Concurrent pin changes refuse before creating
the next fixture. Image automation advanced the initial observed controller to `1ceacb7` during
preparation; that source also contains the workspace record/layout implementation.

The VM setup probe opens from `/state/workspaces/.home`, which the guest creates before starting
its runner even for a replacement legacy disk. Its workspace marker path is separate at the mount
root. Failed suspension does not prevent an attempted UID-scoped Delete; cleanup preserves both
errors and follows the ordinary controller finalizer policy without forcing it.

## First bounded run

At exact head `cdf6f7cc1962abdcb785adfd7d5ffb8dbee6a207`, invocation
[d8d6387e-87f1-4a3a-b73b-fae7fc62598a](https://app.buildbuddy.io/invocation/d8d6387e-87f1-4a3a-b73b-fae7fc62598a)
ran one passed Pod case and one failed VM probe case in 382.51 seconds. The Pod retained state PVC
`ee229d4e-24c8-49be-b77f-1ed0788e26fc` and workspace PVC `42308f53-9ca3-4fb7-b5ec-ac5630240647`
across incarnation `470cee83-c56e-4aaa-a18c-c4db22bf6b19` -> `1c12a3b3-c514-4d7d-8763-f2b0748632fc`.
Both marker checksums remained `7c580d96e343ac90b8475b084a5f88a7da224a1bdfcf69e938240ded86590318`.
The changed fixture claim was refused with `VolumeLayoutMismatch` before replacement. Ownership,
creation record, status binding, actual `/workspace` mount and cleanup all passed. Its backing
filesystem was xfs, correctly unconstrained by `DirectoryTree`.

The raw VM booted and its first setup probe passed (`ext4 /dev/vdb /state/workspaces`), but the
fixture incorrectly expected normal suspend to delete the VirtualMachine object. It timed out at
that unchanged 120-second deletion bound. The controller explicitly reported a halted VM with
disks kept; this is a probe setup error, not a storage-controller defect or slow pull. Normal stop
removes the VMI/launcher and keeps the VM and its DataVolumes. The corrected matrix first proves
ordinary halt/start retains both paths, then explicitly deletes only the recorded fully halted
fixture VM UID to test replacement: Sandbox-owned PVCs must remain, while the VM-owned legacy
DataVolume/PVC is collected. The final probe distinguishes retained marker bytes from expected
legacy absence. No timeout is increased and the already accepted Pod row is not rerun.

Both fixture Sandboxes, templates and PVCs from this run were removed normally; testing storage
returned to 5Gi/80Gi. The legacy row did not run because `--maxfail=1` stopped at the setup error.

## Accepted VM rows

The corrected fixture source is exact head `e39e783d7e8557452dbc71c9141b299edce39cc1` (#10480),
SHA-256 `9c52b593e6405013c3f168ed1f97e3f7acaa0f9b848f85289fc8249d91c4170b` for
`test_storage_deployed.py`. No accepted Pod or model turn was repeated.

| Row                  | Invocation                                                                                                        | Result                                                                        |
| -------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Pod DirectoryTree    | [d8d6387e-87f1-4a3a-b73b-fae7fc62598a](https://app.buildbuddy.io/invocation/d8d6387e-87f1-4a3a-b73b-fae7fc62598a) | Passed row, in the earlier one-pass/one-failure fixture run                   |
| VM RawDiskImage      | [7e47a94a-78f2-478f-af7d-14e474ce615d](https://app.buildbuddy.io/invocation/7e47a94a-78f2-478f-af7d-14e474ce615d) | Passed row and cleanup; later legacy authentication failed in that invocation |
| VM legacy DataVolume | [c0c5046b-52e9-4d80-8544-79a5d7eb0b30](https://app.buildbuddy.io/invocation/c0c5046b-52e9-4d80-8544-79a5d7eb0b30) | One passed, two deselected; 804.55 seconds of pytest                          |

The raw VM kept state PVC `d3d1689b-89f2-49ca-a41e-41177caaedfb` and workspace PVC
`736a7b2d-715d-4f88-bca4-9ad6d09e4ab8` across both ordinary halt/start and explicit replacement
of halted VM `30ae31e4-27e9-4c85-897c-2c175f311e0d` with VM
`c752d4e7-59cb-4428-9f01-5f71860cf4b7`. Each old launcher was gone before the next operation.
Both markers retained SHA-256 `1dae63e8b09c6274ac3ffb213a5613f09576d7dd5c289c3a53cf8690de72ecd8`.
The creation record and status binding retained the same `RawDiskImage/ext4` contract; a changed
fixture claim was refused before starting a replacement. The guest reported
`ext4 /dev/vdb /state/workspaces`, with no partition device.

The legacy VM kept state PVC `50493dac-45db-4c9c-97a6-0990c33864a4`, workspace PVC
`5a4b3e7f-fba8-4c31-90dd-bfd68d48936e` and VM `bb7813fe-5041-4482-8463-1a44eec607b9`
through ordinary halt/start, with both marker hashes unchanged. Explicit deletion of only that
fully halted VM collected its DataVolume/PVC. Replacement VM
`cce642ec-7fa7-44cb-a44d-cb455e1dd646` received workspace PVC
`21358de3-d2e0-4fe6-b795-eae431524715`. The state PVC and hash
`83df2c17e03e55094f5774dd8e5d7f0c4651be88113536aa11972f8a3869bb6a` remained; the workspace
marker was absent as expected. The new workspace was again ext4 on the whole `/dev/vdb`.

Every accepted row's JSON has `passed: true` and `cleanup_complete: true`. All owned fixture
Sandboxes, templates and PVCs were removed through normal lifecycle operations; the final testing
inventory contained only `postgres-1`, with storage quota back to 5Gi/80Gi. No finalizer was removed,
timeout increased, shared service restarted, staging resource written, or existing Sandbox dropped.

Actual tested pins were controller `devel-20261014082718-1ceacb7`, Pod runner
`devel-20261014052218-66a1b03`, VM guest `devel-20261014055155-9642b3d`, and egress sidecar
`devel-20261012182234-b905469`. VM wheel provenance is recorded above. Fresh provisioning and
these replacement boundaries are accepted; BlockFilesystem, kind conversion, node portability,
controller crash recovery and staging acceptance remain outside this live claim.

## Host suspension and the intervening 401

After the raw row passed, the first legacy Session request in `7e47a94a` returned HTTP 401.
At `2026-10-14T19:03:45Z`, the app logged `TokenReview` rejection with reason `unauthenticated`
and no username. This is the app authentication boundary, distinct from its 403 identity-policy
refusal and the historical rollout 403 investigation.

Host `rugged`'s `systemd-suspend.service` journal records freezing `user.slice` at 09:01:59Z and
resuming at 18:59:14Z on 2026-10-14: 9 hours, 57 minutes, 15 seconds. The pytest process had cached
its session-scoped, requested 1800-second testing token before that sleep. Bazel's wall elapsed time
was 36607 seconds while pytest measured 732 seconds. No bearer or JWT claims were retained, so the
raw expiry claim and Kubernetes error detail are unavailable; the observed host pause and stale
short-lived credential explain this authentication failure. A fresh pytest process minted a fresh
token and ran only the remaining legacy row successfully, with unchanged code, bounds and auth
policy. No automatic auth retry or longer token lifetime was added.
