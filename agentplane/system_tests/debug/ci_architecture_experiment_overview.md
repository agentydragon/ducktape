# Agentplane CI architecture: experiment index

Canonical index for [draft PR #16102](https://github.com/agentydragon/ducktape/pull/16102),
reviewed through source
[`711463739730`](https://github.com/agentydragon/ducktape/tree/7114637397306a4883eeacedf0bb0a7e20817179)
on 15 November 2026 UTC. The broader optimization work is paused; documentation
and branch maintenance do not resume it. [Tracker #16099](https://github.com/agentydragon/ducktape/issues/16099)
holds measurement follow-ups.

**No qualified 30% reduction in complete PR feedback has been established.** Two
frozen system-subtree comparisons are 35.4% and 35.2% lower, with matching cases
and passing declared resource-proxy screens. They do not measure representative
PR selection, rerun-inclusive median/p90, fleet reliability or actual KVM cost.
Percentages from different experiments are not additive.

Status means **active** implementation retained on the experimental branch, **adopted**
in devel, **rejected** for the specific tested arrangement, or **parked** pending
qualification. Active does not mean approved for merge or ongoing optimization.
Detailed notes are historical records at their stated sources; their old “next”
steps do not override this index.

## Current implementation and strongest evidence

The [ordinary partition record](ordinary_partition_graph.md) owns the current
roster: 15 App + 7 native Pod cases, 3 MCP + 4 native central-service cases, devel's
newer delegated-Action case on its full App-backed fixture, unchanged complete
VM acceptance on KVM, and unchanged Firecracker cases without incidental
Notification deployment. The full 120-second Gateway budget and App-dependent
History-outage witness remain. Bazel dependencies own ordinary selection; the
journal substitution policy stays shadow-only.

At frozen source
[`22014e7b6c09`](https://github.com/agentydragon/ducktape/tree/22014e7b6c09c64505df438ffec20246ae3a3946),
the same 50 canonical cases pass in both arms:

| Order   | Control                                                                               | Candidate                                                                             | Observed native Bazel delta |
| ------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | --------------------------- |
| B7/A7   | [592.550s](https://app.buildbuddy.io/invocation/8e619948-6bb0-4f75-8160-7badee6be833) | [382.619s](https://app.buildbuddy.io/invocation/e4dff4dc-56ca-450a-9fa9-fd97388b549c) | 35.4% lower once            |
| A8b/B8c | [555.785s](https://app.buildbuddy.io/invocation/5950e993-0f67-4c8c-acbf-7b90cdd6f328) | [360.143s](https://app.buildbuddy.io/invocation/f504b2b7-16c2-4fb7-b5b0-135ce135f748) | 35.2% lower once            |

Exact ActionResult/XML audits and requested-resource screens pass. Placement,
partitioning, Pod capacity and baseline warmth differ; actual KVM CPU/memory are
unknown. Both pairs retain the full Gateway budget. Notification readiness is
about 0.24s in both controls, so its removal does not explain the whole delta.
The later delegated-Action case is outside this frozen comparison.

Historical ordinary CI at
[`5ffba1098176`](https://github.com/agentydragon/ducktape/tree/5ffba10981765cd7b843b85657803cff9282b6ed)
passes all three required checks. [Invocation 7246b046](https://app.buildbuddy.io/invocation/7246b046-25cd-55ff-8375-4b12f100785c)
tests merge `812a87a701ee2de6877d08946d21576b5e167f8d`: 2,012 passed targets,
1,967 cached results and 45 fresh TestRunner actions. Its 48 fresh deployed cases
match the benchmark; the other two unchanged fixture unit cases pass from cache.
This is one full-sweep PR, not a selected-backend baseline or proof for later heads.

After rebasing onto devel `79db05f39e2e6c8c08ad8f9a2dfb8680d81e4d16`, source
`7114637397306a4883eeacedf0bb0a7e20817179` passes five focused test targets plus the
named central-sampler scenario library, including lint, in
[7ddb93cb](https://app.buildbuddy.io/invocation/7ddb93cb-5819-4937-8d9e-ded097240b3c).
This includes the retained delegated-Action case, current MCP provenance assertion,
Thread routes, History reader and renderer. All three required hosted checks also
pass at that head, including ordinary test invocation
[6a98412b](https://app.buildbuddy.io/invocation/6a98412b-cdc0-5789-ba75-5fc4c048b46b).
Its case/artifact denominator has not been independently audited here. These
checks establish current-head correctness, not a matched latency saving. See the
PR for newer-head check status.

## Attempt inventory

| Attempt                                                                       | Status                              | Outcome and limit                                                                                                                                                                                        | Evidence                                                                                                                                                                               |
| ----------------------------------------------------------------------------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Ordinary Pod/Services partitions, native platform driver and KVM VM placement | Active                              | Implemented composition above; two positive frozen screens. Whole-PR qualification remains.                                                                                                              | [Current graph](ordinary_partition_graph.md), [driver migration](common_backend_contracts.md#native-platform-dependency-migration)                                                     |
| Real-process recovery, journal/admission and discarded-reply contracts        | Active; recovery port adopted       | Earlier 25 fresh actions / 100 case exposures pass; lost-journal, collision and receipt-replay defects are detected. No controller/guest equivalence claim.                                              | [Iteration 4](test_architecture_iteration4.md), [ownership](test_architecture_contract_ownership.md)                                                                                   |
| Two real Gateways during active work                                          | Active                              | Positive and stale-boot controls work. Branch-introduced exit-before-output-drain defect repaired; three fresh full-module runs pass 36 cases.                                                           | [Contract](common_backend_contracts.md#two-real-gateways-with-active-executor-work), [repair run](https://app.buildbuddy.io/invocation/9c3eb91e-e7f8-43fc-927a-576607ad458d)           |
| Direct packaged Claude/Codex guests                                           | Active                              | Real kernel/Runtime, two turns, setup/fencing, CLI routes, credential revocation and private systemd proofs pass with controls. Real issuer/controller/KubeVirt/PVC/App/OOM witnesses remain.            | [Guest record](test_architecture_guest_session.md), [iteration 6](test_architecture_iteration6.md)                                                                                     |
| Exact-owner journal substitution selector                                     | Parked; shadow-only                 | Frozen synthetic native median 29.3% lower, unequal cache states and incomplete replacement ownership; eligible for 0/30 historical changes. It removes no required deployment tests.                    | [Selector record](ordinary_contract_graph.md)                                                                                                                                          |
| Historical affected-target replays                                            | Parked                              | Runtime replay selects the same 51 tests. Synthetic MCP-only cut selects 18 → 15; no isolated MCP-only PR in the historical frame. Other 29 base/head pairs unexecuted.                                  | [Iteration 4](test_architecture_iteration4.md)                                                                                                                                         |
| Historical smaller Pod/VM composition                                         | Parked                              | Native mean 17.4% lower, known CPU 5.4% higher; full VM controls cold, candidates warm. VM case work itself is not faster.                                                                               | [Iteration 5](test_architecture_iteration5.md), [curated results](test_architecture_graph_screen_376.json)                                                                             |
| Earlier complete 49-case composition                                          | Rejected                            | 11.2% slower; kernel panic plus distinct successful VM execution both count. CPU/requested-CU screens fail; shortened budget changes exposure.                                                           | [Exact-source screen](common_backend_contracts.md#exact-source-slice-qualification)                                                                                                    |
| Earlier Pod partitions and four-/six-slot capacity variants                   | Parked                              | Complete selected graphs improve 13.4–25.6% under different conditions; one fails its CU-time cap. The isolated 38.9% Pod result is not whole-graph feedback. Superseded bindings are removed.           | [Capacity screens](common_backend_contracts.md#pod-partition-and-capacity-screen)                                                                                                      |
| Remove unused App/Runtime/MCP fixture inputs                                  | Active; several ports adopted       | Smaller dependency closures, unchanged retained scenarios. Three recovery migrations pass; History-outage migration fails its App-stability assertion and is rejected.                                   | [Recovery fixtures](common_backend_contracts.md#ordinary-native-recovery-fixtures), [profiles](unused_mcp_edge_fixture_profiles.md)                                                    |
| Idle native-client PID 1 teardown repair                                      | Active                              | TERM now exits promptly; one cleanup exposure falls 65.057s → 26.884s. No fleet reliability/feedback claim.                                                                                              | [Shutdown record](common_backend_contracts.md#native-fixture-client-shutdown)                                                                                                          |
| Notification-free Firecracker                                                 | Active in combined graph            | Isolated pair 9.61% lower once, below its screen; only 1.6475s of named Notification work observed. Full improvement is not attributable to removal.                                                     | [Dependency screen](common_backend_contracts.md#firecracker-notification-dependency-screen)                                                                                            |
| Existing KVM capacity/concurrency                                             | Parked measurement lane             | Four fresh VM/Firecracker actions overlap on two workers; worker creation-to-ready 188s. Ordinary CI later queues at the three-worker maximum. Cold/four-PR bursts and actual physical cost unqualified. | [Trial](vm_kvm_existing_pool_2026_11_12.md), [ordinary capacity](ordinary_partition_graph.md#resources-and-remaining-qualification)                                                    |
| Prepared Firecracker baseline / fresh private data                            | Parked                              | One pair 8.79% lower; shared-data v1 ownership blocks adoption. Fresh private ext4 store passes, Docker-overlay store fails bootstrap; retained private-data v2 unmeasured.                              | [Prepared graph](ordinary_contract_graph.md), [private-data proof](common_backend_contracts.md#private-fresh-kvm-prerequisite)                                                         |
| PKI-only seed / stopped hosted seed                                           | Parked                              | K3s 10.139s + PKI 15.178s lack enough measured critical-path headroom. Plain-property KVM probe works; no cross-action seed restore saving.                                                              | [Seed audit](ordinary_contract_graph.md)                                                                                                                                               |
| Bridge eight shards                                                           | Parked                              | Same 24 cases; selected-target mean 24.9% lower, worker CPU 62.8% higher. Ordinary graph retains four shards.                                                                                            | [Sharding screen](../../app/debug/2026_11_13_bridge_sharding.md)                                                                                                                       |
| Bridge bytecode precompile                                                    | Parked                              | Warm selected-target saving 9.840s / 11.2%; cold compile costs 4,202.992 CPU seconds and adds 83.444s. Not adopted.                                                                                      | [Bytecode screen](../../app/debug/2026_11_13_bridge_bytecode.md)                                                                                                                       |
| Pair-scenario overlap / five-second kubelet cadence                           | Parked                              | Overlap screen mixed. Secret recovery 23–29s versus 22/29/91s controls; warmth/cost unresolved. Ordinary defaults restored.                                                                              | [Iteration 3](test_architecture_iteration3.md), [MCP overlap](mcp_case_overlap_2026_11_12.md)                                                                                          |
| Pod annotation nudge                                                          | Rejected                            | No observed recovery improvement; withdrawn. Same-Pod Secret projection witness retained.                                                                                                                | [Iteration 2](test_architecture_iteration2.md)                                                                                                                                         |
| Short Gateway exhaustion deadline                                             | Parked (45s); rejected (5s)         | 45s passes unchanged recovery assertions but reduces default-duration exposure; 5s fails real recovery. Ordinary budget remains 120s.                                                                    | [Iteration 2](test_architecture_iteration2.md), [later screen](ordinary_contract_graph.md)                                                                                             |
| Three Dispatcher shards                                                       | Rejected                            | Longest shard 233.3s versus ordinary 218.0–246.8s; CPU grows to 799.3s from 301.4–388.6s with three stack setups.                                                                                        | [Selector/retained-tail screens](ordinary_contract_graph.md)                                                                                                                           |
| Overlap PodTemplate image imports with readiness                              | Adopted fixture work                | One matching-runfiles setup pair 140.131s → 125.428s, 21 cases pass. No causal whole-gate delta.                                                                                                         | [Import overlap](pod_template_image_import_overlap_2026_11_12.md)                                                                                                                      |
| Skip already usable OCI import transactions                                   | Adopted                             | Warm setup mean 46.992s → 32.216s with digest/pin/unpack guards; fleet feedback unmeasured.                                                                                                              | [Usability record](usable_import_transactions_2026_11_12.md)                                                                                                                           |
| Batch current Pod/tmpfs imports                                               | Rejected                            | Corrected clean pair: imports 0.606s faster, action 4.225s slower, CPU 9.1% higher. Earlier cold-store failure was a CoreDNS pull race.                                                                  | [Batch comparison](current_import_batch_comparison.md)                                                                                                                                 |
| Batch current VM/disk imports                                                 | Parked                              | Four matching-input actions: imports 15.1% lower, import-test execution 18.0% lower, CPU 16.1% lower. Production/deployed/full-graph trial unperformed.                                                  | [Disk-backed screen](current_import_batch_comparison.md#completed-disk-backed-vm-screen)                                                                                               |
| Smaller VM guest root                                                         | Parked; no-harness variant rejected | Codex-retaining root 26.2% smaller; one action only 4.0% faster with worse readiness. No-harness root cannot start embedded Execution.                                                                   | [Iteration 7](test_architecture_iteration7.md)                                                                                                                                         |
| Read-only direct guest image binds                                            | Active                              | Both unchanged targets pass four arms; lower candidate action times/CPU have first-arm warming confound. No complete-feedback claim.                                                                     | [Mount record](2026_11_13_guest_mounts.md)                                                                                                                                             |
| Move private systemd proof into direct guest                                  | Parked composition                  | Eleven subtests and omission control work; warm combined means 202.192s → 200.769s, within readiness variation. Default deployed proof remains.                                                          | [Iteration 8](test_architecture_iteration8.md), [iteration 9](test_architecture_iteration9.md)                                                                                         |
| Run private proof in earlier oneshot                                          | Rejected by source audit            | Needs actual Runtime invocation/lifetime/preparation ordering; no execution trial. Observed 22.51s cannot supply 30% alone.                                                                              | [Supervision audit](vm_early_supervision.md)                                                                                                                                           |
| Share Firecracker guest across trust/live-state cases                         | Parked                              | Four actions / six cases pass; queue/import exposure differs and independent fresh fixtures become one compound fixture. No qualified saving or isolation adoption.                                      | [Reuse experiment](test_architecture_firecracker_guest_reuse.md)                                                                                                                       |
| Batch seven renderer profiles                                                 | Rejected                            | Assertions pass, render window/action not shorter; VM-only consumer gains nine outputs. Patch withdrawn.                                                                                                 | [Pre-cleanup record](https://github.com/agentydragon/ducktape/blob/7114637397306a4883eeacedf0bb0a7e20817179/agentplane/system_tests/debug/ci_architecture_experiment_overview.md#L112) |
| Reliability repairs discovered while measuring                                | Adopted; branch test repairs active | Listener allocation, storage precondition, Quiesce and mount ordering have focused proof; Quiesce forwarding recovers about 1.008s later. No measured fleet flake-rate reduction.                        | [Storage](storage_controller_startup.md), [Quiesce](firecracker_quiesce_retry.md), ports below                                                                                         |

## Independent ports in devel

Earlier fixture/import/profile/listener ports, storage readiness
[#16299](https://github.com/agentydragon/ducktape/pull/16299), selected-label archive
[#16316](https://github.com/agentydragon/ducktape/pull/16316), Sampler TokenReview/History outage
[#16317](https://github.com/agentydragon/ducktape/pull/16317), Quiesce
[#16344](https://github.com/agentydragon/ducktape/pull/16344) and route diagnostics
[#16363](https://github.com/agentydragon/ducktape/pull/16363) are inherited from devel.
So are Notification fixture
[#16370](https://github.com/agentydragon/ducktape/pull/16370), native recovery
[#16374](https://github.com/agentydragon/ducktape/pull/16374), Actions mount ordering
[#16386](https://github.com/agentydragon/ducktape/pull/16386),
retained-session resume retry
[#16393](https://github.com/agentydragon/ducktape/pull/16393), launch-fence cancellation
[#16411](https://github.com/agentydragon/ducktape/pull/16411), History reader atomicity
[#16450](https://github.com/agentydragon/ducktape/pull/16450), and App Thread-route contracts
[#16454](https://github.com/agentydragon/ducktape/pull/16454).
Their value does not depend on merging this architecture branch; they are not
counted as branch-only achievements or qualified CI latency savings.

The cleanup retains devel's public `SessionEventReader` invalid-page regression
and removes the duplicate `SessionEventPages` case. Both exercised rejected-page
atomicity (entries, cursor and pagination availability); the retained case goes
through the public reader. No contract or experimental target is retired by this
cleanup, and no timing/reliability gain is credited to it.
The changed client target passes freshly in
[b7076e0a](https://app.buildbuddy.io/invocation/b7076e0a-4d52-4b1f-b4d0-fa68f32ab8f9),
using `7114637397306a4883eeacedf0bb0a7e20817179` plus the cleanup overlay.

## Evidence navigation and follow-up boundaries

- [Assertion ownership](test_architecture_contract_ownership.md) lists retained
  platform/client obligations. [Common-backend chronology](common_backend_contracts.md)
  retains source-specific failures, controls, cleanup and resource accounting.
- Historical milestones: [iteration 2](test_architecture_iteration2.md),
  [3](test_architecture_iteration3.md), [4](test_architecture_iteration4.md),
  [5](test_architecture_iteration5.md), [6](test_architecture_iteration6.md),
  [7](test_architecture_iteration7.md), [8](test_architecture_iteration8.md),
  [9](test_architecture_iteration9.md). JSON evidence beside those notes remains.
- Setup investigations: [baseline census](baseline_reuse_census_2026_11_12.md),
  [setup/case inventory](setup_and_case_shortening_2026_11_12.md),
  [Firecracker image-store reuse](firecracker_image_store_reuse_2026_11_12.md),
  [VM host crashes](vm_guest_progress_and_host_crashes_2026_11_12.md),
  [KVM suite comparison](vm_kvm_required_suite_comparison_2026_11_12.md),
  [burst-capacity record](vm_kvm_capacity_burst_2026_11_12.md).
- [Pre-cleanup overview](https://github.com/agentydragon/ducktape/blob/7114637397306a4883eeacedf0bb0a7e20817179/agentplane/system_tests/debug/ci_architecture_experiment_overview.md)
  preserves the earlier narrative. [Frozen screen source](https://github.com/agentydragon/ducktape/tree/22014e7b6c09c64505df438ffec20246ae3a3946)
  retains retired manual bindings. Later rebases must not relabel those results
  as measurements of the current graph. Raw test artifacts remain private;
  notes contain reviewed summaries and immutable invocation/source pointers.
- [Published architecture report](https://agentydragon.github.io/ducktape/agentplane-test-architecture/)
  has a separate cutoff: report PR #16391, gh-pages
  `51dae409262bb1839caf876710258e52720450fd`, before the later partition screens.
  Branch cleanup does not refresh that published snapshot.

The remaining decisive measurements are actual source/test/BUILD base/head
selection, complete rerun-inclusive feedback, cold/concurrent exposure, and
managed/KVM resource cost. In ordinary run `7246b046`, all three KVM workers are
occupied at the maximum: Firecracker waits 144.641s and VM 94.300s. The competing
managed tail bounds removing all KVM wait to about 120s in that run. Faster
scale-up cannot remove that saturated-window wait. Broader prepared disposable
infrastructure remains untried end to end; PKI-only caching lacks headroom.
Neither diminishing returns nor the complete-feedback adoption standard has
been demonstrated. Resume new experiments only when the optimization work resumes.
