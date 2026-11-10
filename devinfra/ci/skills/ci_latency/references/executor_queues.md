# Executor routing, queueing and capacity

Use this guide to explain how much CI feedback time is waiting versus doing work,
which scheduler owns the wait, and what would shorten the required path. An executor
comparison includes admission, startup, transfers, execution and finalization at
representative concurrent load, not just an isolated test duration.

## Reconstruct the actual execution path

Not every job traverses every layer. Local `bbr` has no GitHub job; a non-Bazel
GitHub check may use no BuildBuddy compute. Record each hop that the selected work
actually uses, with its job, parent/child invocation and physical execution IDs.

| Layer                                      | Establish routing and eligibility from                                                                                                            | Distinguish these waits                                                                                                                      |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| GitHub Actions                             | Workflow/job attempts, `runs-on`, runner group/labels, `needs`, concurrency groups, matrix limits, environment/fork gates and owner-wide activity | Prerequisites or policy versus an eligible job awaiting a GitHub-hosted or self-hosted GitHub runner                                         |
| BuildBuddy Remote Bazel / Workflows runner | `bbr`/`bb remote` command, `.bbrc`, CI overrides, outer execution and timestamped allocation/startup events                                       | Remote runner admission versus provisioning, checkout/tool preparation and Bazel startup                                                     |
| BuildBuddy managed RBE                     | Inner action's effective platform, account routing/entitlements and native execution record                                                       | Managed/shared service or account capacity, including free-tier constraints where applicable, versus worker preparation and action execution |
| Our self-hosted BuildBuddy RBE             | Effective pool/self-hosted selection, compatible registered executors, advertised resources, task limits and live host/VM state                   | Pool saturation, no eligible worker, resource fragmentation, offline/draining executors, and infrastructure provisioning                     |

A GitHub runner can remain occupied while it waits for BuildBuddy; that interval
belongs to both resources' occupancy but only once to elapsed feedback. An outer
BuildBuddy runner can likewise wait on inner RBE actions. A BuildBuddy-hosted UI
or `remote.buildbuddy.io` endpoint does not prove that BuildBuddy supplied the
action's compute. A self-hosted BuildBuddy executor is not a self-hosted GitHub
runner. Nested K3s `FailedScheduling` inside a running TestRunner is fixture
admission, not evidence that the outer BuildBuddy queue is saturated.

In Ducktape, start discovery at `.github/workflows/ci.yml` and its called workflows,
`.bbrc`, `.bazelrc`, target/platform execution properties and
`devinfra/buildbuddy/docs/remote.md`. Our `kvm-vm` pool is defined by
`cluster/cdk8s/buildbuddy_kvm_worker.py` and
`devinfra/buildbuddy/kvm/worker/executor.yaml`; suite routing lives in
`agentplane/system_tests/k3s/defs.bzl`. Read the inspected revision and live state:
do not carry forward old worker counts, host placement, disk type or concurrency
as current facts. Running VMs are not proof of registered, healthy executors.

For representative slow actions, inspect native GetExecution metadata and decode
the referenced Action and Command with the pinned CLI:

```bash
bbapi execution "$INVOCATION" --json > "$OUT/executions.json"
bb download "$ACTION_DIGEST" --type=Action > "$OUT/action.json"
bb download "$COMMAND_DIGEST" --type=Command > "$OUT/command.json"
```

Use the selected execution's `actionDigest` as `HASH/SIZE` for `ACTION_DIGEST`,
then its decoded `commandDigest` for `COMMAND_DIGEST`. Inspect an allowlist of
`Pool`, `use-self-hosted-executors`, `OSFamily`, `Arch`, isolation, CPU/memory/disk
estimates, image and recycling properties. Reconcile organization defaults and
any routing overrides with the observed executor receipt; unresolved pool identity
stays unknown. Do not publish whole Commands, environments, headers or credentials.

Rule/platform/test execution-group properties can defeat a CLI default override.
Verify emitted properties before waiting for a resource trial; then verify the
actual worker or nested Node resources. Requested compute units are not observed
usable cores, RAM, or safe fixture admission. Outer `--runner_exec_properties`
and inner Bazel action properties configure different work.
See [BuildBuddy platform properties](https://www.buildbuddy.io/docs/rbe-platforms/),
[executor pools](https://www.buildbuddy.io/docs/rbe-pools/) and
[Remote Bazel](https://www.buildbuddy.io/docs/remote-bazel/).

## Establish the limiting capacity

For each consequential pool, retain intended and observed routing, workload
classes, eligible worker count, advertised CPU/RAM/disk and task concurrency,
active work, backlog and observation time. Prefer registered executor/slot evidence
over counting Kubernetes Pods or dividing host cores by an arbitrary task size.
Separate configured, registered, healthy, busy and draining capacity. If these
cannot be observed, state the specific missing evidence rather than calculating
utilization from a guessed denominator.

For the in-cluster `kvm-vm` pool, inspect the [BuildBuddy executor pool dashboard](https://grafana.allegedly.works/d/buildbuddy-executor-pool)
over the investigation window for executor registration, active task stages and
guest CPU, memory and work-filesystem usage. Confirm the scrapes are present and
the dashboard is deployed before relying on it. Its executor-local queue is not
the central scheduler's pool backlog; correlate it with native execution records
and eligible capacity rather than inferring pool-wide waiting from that chart.

Partition by scheduler/service, managed versus self-hosted ownership, pool,
required capabilities, isolation and materially different resource shapes before
aggregating by mnemonic or target. Do not compare an OCI build action with a
privileged KVM test as interchangeable capacity. Establish whether independent
work was eligible and ready; idle workers in another pool may be unable to serve
it. One oversized CPU/RAM/disk request or missing KVM capability can leave a job
waiting while aggregate cluster capacity is idle.

Separate account concurrency/quota from provider shared-capacity contention.
Check whether outer runners and inner actions compete for any common budget;
separate timeline lanes do not imply independent quota. Observer/API rate limits
are separate from execution admission limits.
Verify current account entitlements, documented limits and explicit rejection or
throttling evidence where accessible. A label such as "free tier", a public plan
field, or a plateau in observed activity does not establish the effective limit
or its cause. Record scope/access limits when provider-wide or owner-wide demand
is invisible; do not hardcode tier prices, concurrency or priority guarantees.
Use current [BuildBuddy offerings](https://www.buildbuddy.io/pricing/),
[GitHub limits](https://docs.github.com/en/actions/reference/limits) and
[concurrency policy](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
as references, not evidence of this account's entitlement.

Reconcile queue entries with unique physical executions and the heads still
needing them. Separate action merging/reuse, hedges, retries, superseded or
cancelled work and stale records. Reservation counts are not counts of distinct
useful actions or PRs. Preserve snapshots with timestamps;
changing active views are not a completed cohort.
An old head's execution can still serve a newer head through shared work; verify
remaining consumers before recommending cancellation as a capacity improvement.

## Measure and show the queue contribution

Keep each producer's clock semantics and sampling resolution. GitHub creation to
job start can include dependency, concurrency or environment waits; infer runner
starvation only after those are resolved. The existing `summarize.jq` calls that
interval `queue_seconds`; label it created-to-start delay in reports until its
queue owner is established. For inner actions, native
`queuedTimestamp` to `workerStartTimestamp` measures the recorded queue interval.
Worker preparation through `executionStartTimestamp` includes input transfer and
does not by itself isolate VM boot. Separate fetch, execution and output upload
where their timestamps permit it. Missing, epoch, inverted and future clocks are
unknown, not zero or absolute-valued durations. Keep valid intervals when another
phase is unknown. A stage such as `EXECUTING` does not supply missing phase clocks.
Report unfinished queue ages as censored observations separately
from completed-duration percentiles.

When queues affect the decision, display:

- A pool comparison with the cohort/time window, workload/count, queue p50/p90,
  preparation and execution times, still-queued count/oldest age, observed capacity,
  and unknown attribution/clocks. Keep a live capacity snapshot separate from
  historical percentiles. A mean over mixed pools hides the scarce lane.
- A few linked per-head timelines with separate GitHub, outer BuildBuddy, inner
  action and fixture lanes. Mark prerequisites, waiting, work and unknown intervals;
  show nesting rather than adding overlapping durations into a stacked total.
- Backlog and busy/eligible capacity over the same window when repeated snapshots
  or metrics exist. A single Active-page count is not a utilization time series.
- The queue's contribution to the required-check critical path and how much
  removing it could help before another gate becomes last. Total queued
  action-seconds are not per-head feedback savings. Use conditional headroom only
  with established dependencies/clocks; label the fixed-capacity/cache assumptions.

Use the site's Mantine components and chart framework under the shared reporting
contract. Useful forms include a queue-versus-execution comparison by pool and an
aligned backlog/capacity series. Include units, sample sizes, denominator/unknown
counts and source links; choose what explains the bottleneck instead of filling
every suggested chart. Expose only reviewed aggregate labels, not worker identities.

## Propose a change at the queue's owner

Choose from evidence rather than recommending more cores or workers by default:

| Observed mechanism                                              | Candidate intervention and decisive check                                                                                                                                                                                                                                          |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Eligible GitHub jobs exhaust matching runner/account slots      | Reduce redundant fanout or retire superseded PR work under the existing cancellation policy; compare alternative runner capacity or entitlement. Preserve required checks and release/publish completion. Measure independent eligible backlog and owner-wide occupancy.           |
| Outer managed BuildBuddy runner admission dominates             | Compare account/service capacity, a warm runner floor or routing the outer runner elsewhere. Adding self-hosted inner RBE workers cannot remove an unchanged outer-runner admission bottleneck.                                                                                    |
| A compatible self-hosted pool is saturated                      | Compare more independent workers, reserved capacity or bounded autoscaling with resizing one worker. Honor task-slot limits, host/SSD/KVM constraints and other tenants. Include boot/registration, warmup, idle cost, scale-in admission/drain ownership and the next bottleneck. |
| Queued work has no fitting or healthy executor                  | Repair registration/routing or supply the missing capability; right-size requests using actual CPU, peak working sets, disk and nested admission demand. Do not lower requests just to make the queue disappear or infer delivered capacity from compute units.                    |
| Worker preparation or transfers dominate                        | Compare measured workspace/snapshot reuse, compatible pool/cache affinity, SSD placement, image/input reduction or a cache proxy. Include locality and cold-cache effects; a proxy does not repair scheduler queueing or long scenario execution.                                  |
| Useful execution, serial dependencies or fixture waits dominate | Improve that work or expose justified parallelism. More executor capacity only helps the independently ready work it can serve; trace readiness and fixture resource failures separately.                                                                                          |

For each serious proposal, name the config owner, affected workloads, mechanism,
estimated per-head savings and evidence/assumptions, recurring compute/idle cost,
coverage and lifecycle constraints, and the smallest experiment that could refute
it. Compare combinations when moving work would saturate another pool or consume
GitHub slots longer. Paid/shared and self-hosted options belong in the same
comparison when feasible; available cluster cores alone do not make migration a win.

Test the intended allocation on matching inputs and confirm the effective route,
native properties and actual capacity. Compare queue tails and end-to-end feedback
at representative burst load, with source, cache state and competing traffic
recorded. Keep cold-start and steady-state results separate. Preserve Docker/KVM
isolation, scenarios and assertions; name deliberate coverage changes. An isolated
fast canary proves neither sustainable throughput nor a lower mean complete CI.
Do not retry a whole invocation merely because its log stream failed: reconcile
the existing execution first so duplicate work does not create the queue being
diagnosed. Infrastructure or workflow changes still require the task's authorization.
