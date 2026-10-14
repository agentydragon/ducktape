# Fresh testing storage acceptance

The manual `test_storage_deployed` target uses only uniquely named fixture SandboxTemplates and
Sandboxes in `agentplane-testing`. It verifies the exact accepting controller pin before writing
layout declarations. No deployed template, existing Sandbox, staging resource, guest formatting
implementation or conversion path is changed.

| Fixture          | State                            | Workspace                                                    | Expected after stop/start                                              |
| ---------------- | -------------------------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------- |
| Pod              | Fixed DirectoryTree              | Explicit DirectoryTree at `/workspace`                       | Both PVC identities and exact file bytes retained                      |
| VM selected path | Fixed DirectoryTree via virtiofs | Explicit RawDiskImage/ext4 on a Sandbox-owned filesystem PVC | Both PVC identities and exact file bytes retained                      |
| VM comparison    | Fixed DirectoryTree via virtiofs | Existing VM-owned blank DataVolume                           | State retained; old workspace PVC removed, replacement workspace empty |

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

Implementation and live checks are in progress. No storage matrix row is accepted yet. This proves
fresh provisioning and replacement only: no existing-data cutoff, kind conversion, BlockFilesystem,
controller restart, node portability or staging acceptance is claimed. Rollout and cutoff remain
separate gates in the task DAG.
