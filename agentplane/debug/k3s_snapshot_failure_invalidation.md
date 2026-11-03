# K3s persistent snapshot invalidation on lifecycle failure

The persistent k3s stack writes a dirty marker before it uses the retained daemon and changes the
marker to clean only after fixture cleanup, loop-device release, and trace closure succeed. A failed
setup or teardown can therefore leave executor state that should not be restored as a clean baseline.
The `running_stack` context now wraps its `ExitStack`s in an outer guard: an exception escaping a
persistent stack lifecycle requests snapshot invalidation, then preserves and re-raises the original
error. Successful cleanup does not create the invalidation marker. The guard requires the existing
`AGENTPLANE_K3S_EXECUTOR_PERSISTENCE=1` permission and only applies when `persist_infrastructure`
is enabled. A marker-write error is logged and attached to the original exception.

## Provider contract and workspace root

The official Firecracker provider source at the inspected provider revision mounts the action
workspace at `/workspace` (`firecracker.go`, `guestWorkspaceMountDir`), extracts
`.BUILDBUDDY_INVALIDATE_SNAPSHOT` from the guest, and invalidates the selected snapshot's
`SnapshotKeySet().BranchKey` during VM removal. `runner.go` also treats this marker as a request not
to recycle the runner. Invalidation is by snapshot family key, not an individual snapshot ID, so it
may discard other reusable snapshots in the same family. Generic snapshots are excluded by the
provider. No cluster deployment or KVM workload was used to verify this contract.

The action command has no explicit working directory and starts with
`external/bazel_tools/tools/test/test-setup.sh`. Its `TEST_TMPDIR` and
`TEST_UNDECLARED_OUTPUTS_DIR` are under `/workspace/_tmp/...` and
`/workspace/bazel-out/...`; pytest later changes into the runfiles tree. The implementation finds
the common ancestor of those Bazel paths, validates their expected top-level directories, and
requires the declared `test-setup.sh` input there. It does not infer the action root from pytest's
cwd or require undeclared repository source files.

A harmless uniquely keyed TestRunner probe verified that this root can be written: invocation
[`32c730fc-d623-4780-9c3c-07a22a1018e2`](https://app.buildbuddy.io/invocation/32c730fc-d623-4780-9c3c-07a22a1018e2)
reported `/workspace/.BUILDBUDDY_INVALIDATE_SNAPSHOT`. A same-key follow-up on the same physical
worker, [`e1688388-be6c-4065-bb98-be9002c51ae6`](https://app.buildbuddy.io/invocation/e1688388-be6c-4065-bb98-be9002c51ae6),
ran on a new VM with no prior task and reported that the sentinel was absent. This supports that the
marker reached the provider and prevented reuse for this probe family; it does not prove details of
provider snapshot persistence beyond those observations.

## Failure evidence boundary

Recent PR CI attempts failed during persistent baseline cleanup before any VM scenario ran. The
teardown evidence records `StopPodSandbox` waiting 60 seconds for an old container to stop. The
recurring symptom alone does not establish whether the original state was a paused shim, a process
wedge, or a provider/guest issue. The guard addresses the state-safety boundary after a stack
lifecycle error; it does not diagnose that cleanup stall or claim that snapshot reuse caused it.

The guard is intentionally limited to persistent stack lifecycle exceptions. Normal scenario
assertions are consumed by pytest outside the session fixture's setup/teardown context; successful
fixture cleanup can still certify a clean baseline. Cleanup or certification failures escape the
stack context and invalidate the family. A direct production-family invalidation was not run as part
of this change. The focused Bazel test passed at
[`0ae1f4e7-c93b-4de6-877e-9da585bd4650`](https://app.buildbuddy.io/invocation/0ae1f4e7-c93b-4de6-877e-9da585bd4650),
and the separately named `stack` target passed its mypy aspect at
[`77184a96-1822-4ef2-bdf9-7030fbc77d8b`](https://app.buildbuddy.io/invocation/77184a96-1822-4ef2-bdf9-7030fbc77d8b).
