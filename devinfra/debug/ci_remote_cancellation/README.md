# CI cancellation propagation

The [active investigation](investigation.md) tracks remote action work after
cancellation, the unconfirmed hosted lease duration, competing explanations, and
the next evidence to collect. The [2026-10-28 protocol probe](admission_fence_probe.md)
is a separate control of the cancellation API's scope.

Related evidence in this folder:

- [Retry-disabled comparison](retry_comparison.md): `--remote_retries=0`
  suppressed a plain-action requeue, but a different action released after
  cancellation still completed; [selected evidence](retry_comparison.json).
- [Normal cancellation reproduction](normal_path_reproduction.md): the
  2026-10-28 plain-action and analysis-barrier probes both completed new remote
  work after cancellation; [selected evidence](normal_path_reproduction.json).
- [Action merging and hedging hang](action_merge_hedge_hang.md): the 2026-10-04/05
  incident, execution identities, source trace, and upstream report draft.
- [CLI retry during the VM shutdown probe](vm_shutdown_retry.md): the 2026-10-27
  transport retry that reused an inner invocation ID across outer runs.
- [Continued VM action evidence](continued_action.json): the selected worker and
  test-result fields supporting the active investigation's incident timeline.

## Original signal-forwarding investigation (2026-10-10)

The bb-remote composite ran `bb remote | tee` as a foreground pipeline. GitHub
signals the step's entry process first; Bash waits for the foreground command
instead of forwarding that signal. Run the command in the background and trap
signals around the interruptible `wait` builtin in the entry shell.

That alone did not cancel the child RBE action in a live probe. The pinned
[BuildBuddy CLI](https://github.com/buildbuddy-io/buildbuddy/blob/ac30620d7a5ead46407ccf1a2c68492389f489c2/cli/remotebazel/remotebazel.go)
requests cancellation of its **outer** invocation on interruption. Also request
`CancelExecutions` for the child IDs supplied by the workflow before launch.
This avoids relying on buffered logs or the CLI's post-completion invocation file.
The main Bazel CI workflow already supplies run-and-attempt-specific test/build IDs.

## Live probe

Run a temporary `genrule` with `sleep 90; echo completed > $@` through `bbr`,
forcing execution with `--noremote_accept_cached`. Assign an explicit child UUID.
Poll `GetExecution` until the child action is `EXECUTING`, then send SIGINT to
only the local Bash entry PID (not its process group). Inspect it four seconds
later. Probe cleanup explicitly cancels that UUID **after** recording the result.

| Wrapper                                  | Child invocation                       | Entry shell after 4s | Child action after 4s                       |
| ---------------------------------------- | -------------------------------------- | -------------------- | ------------------------------------------- |
| Foreground pipeline                      | `36037e21-3ec0-41a4-a1ac-d725d254d507` | Still running        | `EXECUTING`                                 |
| Signal forwarding only                   | `8f874187-e5d2-4ba8-9a24-7518b3949470` | Exit 130             | `EXECUTING`                                 |
| Forwarding + explicit child cancellation | `e6a3a7b4-4e65-4d5e-aa78-8f89f2717e88` | Exit 130             | `COMPLETED`, code 1, `invocation cancelled` |

These are single observations, not a latency distribution or proof of physical
worker teardown. Cancelled execution metadata had no useful worker timestamps.
Two intervening probes failed during local API connection/DNS lookup before the
measurement; they are inconclusive, not successful cancellations.

The regression targets `//devinfra/ci:test_bb_remote_log` and
`//devinfra/ci:test_cancel_bb_invocations` passed in
[ea66edc0-f550-4f42-9c67-0dbcfe599107](https://app.buildbuddy.io/invocation/ea66edc0-f550-4f42-9c67-0dbcfe599107).
They cover SIGINT/SIGTERM sent to the entry process, normal command status and
complete log capture, the exact child-ID request set, empty IDs, and API errors.

## Limits

- Callers without `bazel_invocation_ids` get outer signal forwarding only.
- Cleanup is best effort within the
  [GitHub cancellation grace period](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation).
  Child requests run concurrently with four-second socket timeouts; GitHub can
  still kill the process if DNS or the outer CLI cancellation blocks.
- This does not add a sweeper for already orphaned runs. Measure real superseded
  CI runs after merge to determine the reduction in wasted worker time.
