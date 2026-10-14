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

Implementation and live VM checks are in progress; the Pod row passed as recorded below. This proves
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
