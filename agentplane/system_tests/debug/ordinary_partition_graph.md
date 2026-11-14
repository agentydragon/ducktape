# Ordinary partitioned system-test graph

The architecture branch implements the tested system-subtree composition as
ordinary Bazel targets. Exact-head ordinary CI passes at `5ffba10981`, including
the partitioned cases and affected build/aspect checks. Representative PR
comparisons, cold/burst capacity and whole-PR reliability remain unqualified.
This does not establish the 30% complete-feedback goal.

## Coverage and selection

| Ordinary target                    | Preserved behavior                                                        | Environment                                                                 |
| ---------------------------------- | ------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `suites:test_pod`                  | Fifteen App Session, History, storage, instruction and lifecycle cases    | Real App-backed K3s stack; six bounded case leases; storage exclusive first |
| `suites:test_pod_platform`         | Seven workload-trust, certificate-reissue and Egress-credential cases     | Real controllers, issuers, Gateway and proxy; native fixture identities     |
| `suites/mcp:test_services_mcp`     | Three discovery, consented-identity and Policy-binding cases              | Real App/MCP deployment, OAuth fixtures and Action Service                  |
| `suites/mcp:test_services_central` | Four setup, retained-answer, suspend/resume and Gateway-recovery cases    | Real central services and Pod-bound native readers                          |
| `suites:test_vm`                   | Unchanged complete VM scenario, both harnesses and process-trace contract | Fresh K3s/KubeVirt stack with hardware acceleration                         |
| `suites:test_firecracker`          | Unchanged wrong-issuer and checkpoint/thaw cases                          | Fresh K3s/Firecracker stack without incidental Notification deployment      |

Labels are relative to `//agentplane/system_tests`. Other ordinary system targets,
including the full 120-second Gateway budget and the App-dependent History-outage
witness, remain required through normal affected-target selection.

The Pod bindings have a disjoint exact union of 15+7 original cases; Services
has a disjoint exact union of 3+4. The retained App Pod case function ASTs and
resource owner are unchanged. The three promoted native/MCP modules preserve
their candidate function ASTs. VM and Firecracker source files and the extracted
MCP provenance helper are byte-identical to `bf57990ed4`; the helper retains all
30 assertions inherited from devel. These are source audits, not executed-case
or complete semantic-equivalence proof.

Each ordinary target collects its own complete roster. The former combined
Services entrypoint, duplicate Pod bindings and superseded manual partition/
capacity targets are removed. Historical experiments remain reproducible at
their immutable recorded source, including `22014e7b` on
`agent/ci-system-screen-22014e7b`.

The ordinary Bazel dependency graph owns selection. There is no directory-based
coverage exemption or new permission to ignore BUILD changes. CI still runs
normal Bazel-diff selection, retains affected builds, and uses its existing full
sweep for graph-wide inputs. The older journal substitution policy remains
shadow-only; its synthetic result is not credited to this implementation.
The existing Bazel-diff package-default fingerprint limitation remains a
qualification concern, not a newly solved problem.

## Evidence motivating implementation

At immutable `22014e7b`, the original 14-target and candidate 16-target system
graphs passed the same 50 canonical JUnit cases. In the B/A exposure, native
Bazel elapsed was 592.550s versus 382.619s, 35.4% lower once. Its reviewed physical
action/JUnit results pass the declared requested-resource screens, with actual
KVM CPU/memory unknown and unequal Pod setup warmth.

The reversed A/B exposure also completed successfully: control
[`5950e993`](https://app.buildbuddy.io/invocation/5950e993-0f67-4c8c-acbf-7b90cdd6f328)
took 555.785s, candidate
[`f504b2b7`](https://app.buildbuddy.io/invocation/f504b2b7-16c2-4fb7-b5b0-135ce135f748)
360.143s, 35.2% lower once. Both pairs have matching canonical case/outcome
multisets and verified ActionResult/XML digests. All declared resource-proxy
screens pass. Both retain the full 120-second Gateway budget; the separate
45-second experiment is absent from their commands. Notification readiness is
about 0.24 seconds in both controls, so removing it does not explain the whole
improvement. Placement, partitioning, Pod capacity and uncontrolled warmth
remain material differences. Neither pair measures complete required PR checks,
representative selection, p90 or flakiness.

## Ordinary CI qualification

At `5ffba10981765cd7b843b85657803cff9282b6ed`,
[`7246b046`](https://app.buildbuddy.io/invocation/7246b046-25cd-55ff-8375-4b12f100785c)
tests merge `812a87a701ee2de6877d08946d21576b5e167f8d`, whose parents are devel
`af13289e463c756664559ee3ac0b73bd585c5fd9` and that branch head. All three
currently required contexts succeed: Bazel Test & Build, Pre-commit checks and
Build artifacts + imports check. The Bazel test invocation reports 2,012 passed
targets: 1,967 cached results and 45 fresh physical TestRunner actions. Target
summaries record one attempt each; provider-internal retries are unknown.

Fifteen fresh deployed targets produce 48 passing cases, exactly the deployed
case/outcome multiset from the pinned 50-case benchmark. The benchmark's other
two cases belong to `suites/live:test_conftest`: unchanged fixture unit tests
with a cached PASS in ordinary CI, not two fresh deployed exposures. Every
downloaded deployed XML digest matches its advertised blob and exact physical
ActionResult. The native Gateway module also freshly passes its 12 cases,
including the corrected output-drain contracts. The four new devel Sampler
regression cases, 24 renderer cases and 25 manifest cases pass separately.

The test invocation takes 559.538 seconds; its GitHub Bazel job runs for 654
seconds. These are different clocks, and this full-sweep architecture PR is not
a routine affected-target comparison. BuildBuddy's stored creation timestamp is
ingestion time, later than some action starts, and must not be used as test start.

One read-only current-graph query at this head confirms the six promoted target
labels and follows real changed paths from three merged PRs. A test-only Sampler
path has one affected test, production Sampler paths have 21, and mixed Gateway/
driver paths have 47, including all six partitions. These are reverse-dependency
closures, not historical base/head Bazel-diff replays or whole-PR target counts.
The architecture PR itself changes graph-wide inputs and takes the full sweep.

## Reliability repair

Rebased ordinary CI at `bf57990ed4` exposed a branch-introduced test defect:
the new two-Gateway contract stopped reading on `exited`, before the companion
had closed and drained its output. Both test read loops now use the existing
executor completion condition: exited, closed and an empty output page. Exact
suffix, cursor advancement, one process identity and fresh successor-command
assertions remain. The corrected full module passes three fresh runs, 12 cases
each, with no failures, errors or skips in
[`9c3eb91e`](https://app.buildbuddy.io/invocation/9c3eb91e-e7f8-43fc-927a-576607ad458d),
using `bf57990ed4` plus the recorded repair overlay. This repairs this branch's
test; it is not a measured production flake-rate improvement.

The old combined Services target also retained an unused direct protobuf
dependency after its helper extraction. Removing it passes the target and
helper type checks in
[`eb67d122`](https://app.buildbuddy.io/invocation/eb67d122-b54a-46a0-990c-e27776a387cd).
The ordinary partition removes that combined entrypoint entirely.

## Resources and remaining qualification

The App Pod action requests eight compute units and permits six case leases;
the platform partition uses its own fresh stack. VM requests four CPUs, 7 GiB
memory and 50 GB disk; Firecracker requests four CPUs, 6 GB memory and 20 GB disk.
Both use `kvm-vm` without recycled K3s state. Requested resources are reservation
proxies, not physical CPU, concurrent memory or monetary cost.

Source and live state inspected on 14 November show a three-worker KVM maximum,
three ready workers, a one-worker floor, a 120-second scale-up stabilization
window and at most one additional worker per 900 seconds. Each live worker
reserves four CPUs and 8 GiB; the executor advertises one four-CPU/7-GiB slot.
The earlier concurrent trial's 188-second creation-to-ready measurement excludes
waiting before worker creation. Three continuously ready workers reserve 12
CPUs and 24 GiB, compared with four CPUs and 8 GiB at the configured floor;
this is a capacity reservation, not a billing or utilization estimate.

In ordinary CI `7246b046`, Firecracker waits 144.641 seconds before worker start
and executes for 271.636 seconds. VM waits 94.300 seconds and executes for
243.621 seconds. Firecracker finishes at 23:07:13 UTC; the last managed test,
Gateway budget, finishes at 23:05:13. Removing all KVM waiting would therefore
save at most about 120 seconds from this observed test tail, holding other work
fixed: the managed tail would take over. This is a conditional bound, not a
measured scaling improvement or an explanation of the queue's cause. Cold/burst
measurements and actual queue ownership remain necessary.

Remaining acceptance requires:

1. Actual source, test, BUILD and mixed-input Bazel-diff replays on the new graph,
   counting graph-wide fallbacks and retaining the known package-default
   fingerprint counterexample.
2. Complete required-check comparisons with normal caching, queues and reruns;
   cold and concurrent PR exposure; no worse p90 or flake rate.
3. Reviewed managed/KVM resource use and recurring capacity/maintenance cost.

Maintenance consists of keeping scenario ownership and direct dependencies
accurate, retaining fresh per-case state, and qualifying KVM capacity. There is
no new persistent fixture service, lease broker or retained snapshot lifecycle.
