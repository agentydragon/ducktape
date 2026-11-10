# Publication runner: pnpm abort on 2026-10-30

## Impact and recovery

[CI run 41003637896](https://github.com/agentydragon/ducktape/actions/runs/41003637896)
at `e2d26f48` passed Bazel CI but repeatedly failed image publication during
module extension evaluation. `npm_translate_lock` reported
`pnpm install --lockfile-only` exiting 134, with empty stdout and stderr.
Multiple Agentplane images and unrelated release jobs failed before their tests.

Attempt 3 published all Agentplane images. By 05:09 UTC, the sampler, app,
Actions, frontend, History, and runner gateway had updated to `e2d26f48` in
staging and had their desired ready replicas; the staging Flux Kustomization
reported Ready and Healthy.

Recovery is not proof that snapshot invalidation fixed the cause. A targeted
`InvalidateSnapshot` request returned success before attempt 3, but the
successful sampler test and image build both used the old VM lineage and
snapshot-key version. Their previous saved task was an intervening diagnostic
that passed with `bazel --batch` and workspace-rule logging enabled.

## Why the earlier runner tag bump did not cover publication

The [previous profiler incident](2026_10_29_pr12822_profiler_clock.md) changed
`.github/workflows/bazel-ci.yml` from `ducktape-bazel-ci-v3` to
`ducktape-bazel-ci-v4`. That workflow explicitly requests 8 compute units and
that recycling key. Publication calls `.github/actions/bb-remote` with its
4-unit default and no recycling key. `devinfra/bbr.json` also has no explicit
recycling key. The earlier workflow edit therefore did not reset these pools.

The failed publication runner's metadata records creation on October 20,
2026 at 19:09:59 UTC. Its saved snapshot version was 2382, with immutable
snapshot ID `11fb3910-c8c7-4894-8468-c802ad8ada0a`.

## Retained failure and remaining limits

- An ordinary 4-unit `bbr test` reproduced the same 134 failure:
  [inner invocation](https://app.buildbuddy.io/invocation/7b0244c7-9a53-4aa9-b509-b4c0e7204663).
- Shutting down the Bazel server and repeating the test still failed:
  [after shutdown](https://app.buildbuddy.io/invocation/6e3cb9e3-215b-4cdf-ac6b-a2e1eff1806a).
- A new diagnostic recycling key passed the same target:
  [invocation](https://app.buildbuddy.io/invocation/3b62232f-7272-46ed-984e-e4a130ac8fb9).
- Restoring the immutable failed snapshot using `bbr --run_from_snapshot`
  reproduced the failure without changing the live publication snapshot key:
  [outer invocation](https://app.buildbuddy.io/invocation/a9a15fb4-939f-4373-b7d0-a322e9b103a1),
  [inner invocation](https://app.buildbuddy.io/invocation/d394ae12-bbc1-4e3d-afa5-e98f1983ad70).
  Node 22.22.2 and 24.13.0 both ran simple commands in this restored VM.
- Restoring the same failed snapshot, setting the npm extension's `quiet`
  attribute to False, and enabling workspace-rule logging passed. pnpm
  actually ran and completed in 4 seconds:
  [outer invocation](https://app.buildbuddy.io/invocation/516354a4-261a-46e4-8b44-5345439c3665),
  [inner invocation](https://app.buildbuddy.io/invocation/041127fa-0788-437e-af54-22ba9634d254).
  A follow-up on another restore separated the changes: workspace-rule
  logging alone failed (134), `quiet = False` then passed, and reverting
  `quiet` to its original value also passed:
  [outer invocation](https://app.buildbuddy.io/invocation/0bd3ea1a-a8dc-438a-acbb-cb0357136c81).
  This localizes recovery to changing and reevaluating the extension rather
  than workspace-rule logging. It does not yet distinguish the effect of
  `quiet` itself from invalidating retained extension state.

To distinguish a fresh abort from a retained error, another restore wrapped
all three existing Node executables with a script that records invocations.
Bazel reported the same pnpm 134 failure in 0.153 seconds, with no wrapper
invocations:
[outer invocation](https://app.buildbuddy.io/invocation/b29d2b6d-5e14-4243-ac28-2bebd6bf87e6),
[inner invocation](https://app.buildbuddy.io/invocation/fc754608-a3e3-4c06-aa12-0e9d2f8479ac).
This identifies a retained failed extension evaluation as a reason that
retries on this snapshot remain stuck; it is not evidence of a new pnpm
abort on each retry. Changing the extension attributes forces reevaluation
and restores progress, including after reverting the attribute change.

Repeating the wrapper experiment and then shutting down the restored Bazel
server caused a real Node invocation; pnpm completed in 604 ms and the target
passed:
[outer invocation](https://app.buildbuddy.io/invocation/83ad0eea-7d77-48aa-8e74-e82399a3e5cf),
[inner invocation](https://app.buildbuddy.io/invocation/386d9b90-8be3-4c8e-ac95-8ed8fee081ac).
Unlike the earlier shutdown-only experiment, this also changed the child
process launch by inserting a shell wrapper. It confirms that the earlier
wrapper observation was observing a retained evaluation failure, but does
not establish that restarting the server alone reliably fixes it.

A separate reproduction crashed Bazel after the pnpm error at
`CollectLocalResourceUsage.stop` (Bazel 9.2.0, line 212):
[invocation](https://app.buildbuddy.io/invocation/9629e343-037a-4522-87b0-8e095786fa60).
That assertion checks for a repeated stop, unlike the earlier profiler
incident's `TimeSeries.toDoubleArray` duration bounds. At that stage, a shared clock cause had not been established. Empty stderr
and status 134 alone did not establish where or why the abort originated.

## Clock defect and Node abort reproduced

Following the snapshot ancestry found a successful build at snapshot 2374
and a profiler crash at snapshot 2375:
[outer invocation](https://app.buildbuddy.io/invocation/92ae949e-fd15-4087-9da4-67df4d7e1c00).
That build logged `Invalid duration: start=58297809, end=58297794` before
crashing in `CollectLocalResourceUsage.stop`. The next saved task reported
the pnpm 134 error. Replaying these older snapshots did not reproduce every
historical failure; scheduling and the host used for restore vary.

The failed snapshot has the same defect reported in
[BuildBuddy issue 13988](https://github.com/buildbuddy-io/buildbuddy/issues/13988):
its `tsc` clocksource disagrees between vCPUs. Pinning a process to each vCPU
in turn produced 2,000 backward `CLOCK_MONOTONIC` reads, with a worst step
of 7.2 ms:
[invocation](https://app.buildbuddy.io/invocation/33137aef-0843-4284-9a2a-3673cadd7681).
Another restore had nearly 480 ms of skew. Switching that isolated VM to
`kvm-clock` removed all backward reads in the same probe:
[invocation](https://app.buildbuddy.io/invocation/db9338d1-c7c1-47d7-9f17-c45b9fc1ca44).

A minimal Node 22.22.2 timer program then reproduced the native abort. Start
Node on the faster vCPU, wait for its readiness output, and move it to the
slower vCPU while scheduling timers. With approximately 239 ms of skew, it
terminated with SIGABRT (shell status 134):

```text
uint64_t node::Environment::GetNowUint64() at ../src/env.cc:1610
Assertion failed: (now) >= (timer_base())
node::timers::BindingData::SlowGetLibuvNow
```

After switching the same VM to `kvm-clock`, the identical migration and
Node program exited 0:
[invocation with both runs](https://app.buildbuddy.io/invocation/5c06b26b-ba61-410c-8928-51b4ed7b71ae).
This reproduces a clock-induced abort in the exact Node version used by
pnpm on the affected snapshot. The historical pnpm invocation did not retain
a native stack, so that exact stack cannot be recovered from its empty log.

## Why the pnpm error had empty stderr

A separate minimal module extension executed a shell that printed
`ABORT_SENTINEL` to stderr and sent itself SIGABRT. With `quiet = False`,
the sentinel appeared in the console, but the returned execution result was
`rc=134 stdout="" stderr=""`:
[invocation](https://app.buildbuddy.io/invocation/6c8b5570-ccba-4a8d-b011-504360fd5d80).
Bazel 9.2.0's
[StarlarkExecutionResult](https://github.com/bazelbuild/bazel/blob/9.2.0/src/main/java/com/google/devtools/build/lib/bazel/repository/starlark/StarlarkExecutionResult.java)
uses the command result's streams on abnormal termination instead of the
recorder used for ordinary exits. This loses captured output in this case.
With the npm extension's default `quiet = True`, the native assertion is
therefore absent from both the console and the returned stderr.

## Workaround

The repo `.bbrc` configures BuildBuddy's `run-under` wrapper to select
`kvm-clock` before starting the hosted runner (originally in `devinfra/bbr.json`).
Both the GitHub `bb-remote` action and `bbr` use these native defaults, covering
publication, Bazel CI, and arbitrary remote scripts. This runs before a restored Bazel JVM resumes
work; changing the platform property also creates a new snapshot namespace,
leaving behind the retained extension failure.

The wrapper was tested against the immutable failed snapshot. Its command
observed `kvm-clock` from the start and recorded zero backward reads:
[invocation](https://app.buildbuddy.io/invocation/c31c381c-a17e-48ac-b905-56cf836201f4).
Remove this workaround when the upstream snapshot clock defect is fixed and
restored guests have been validated. It does not repair Firecracker's
per-vCPU TSC state.

The focused `//devinfra/buildbuddy:test_bbr` check passed through the new wrapper:
[invocation](https://app.buildbuddy.io/invocation/bc492f6d-dba9-4c88-b486-29556cba5c0d).
