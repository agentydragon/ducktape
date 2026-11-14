# Agentplane CI architecture: experiment overview

Evidence reviewed on 14 November 2026 UTC. Implementation measurements below
identify their source; the historical frozen graph screen uses `38f6c8e723` on
[draft PR #16102](https://github.com/agentydragon/ducktape/pull/16102).
Optimization resumed with a 30% median complete-feedback qualification goal and
no worse p90 or flake rate. This write-up records completed work and outstanding
evidence; the replacement gate remains unqualified.

**There is no qualified 30% reduction in complete PR feedback.** The branch has
working smaller contracts, useful independent ports and several rejected ideas.
The historical synthetic graph screen has a 29.3% lower median native Bazel time,
with unequal cache states and incomplete replacement coverage. The historical
combined graph was 17.4% faster with unequal baseline warmth and higher known CPU.
Neither establishes a rerun-inclusive PR saving.

The latest fixed-source full-system screen is negative: 14 ordinary targets versus a 16-target
composition pass the same 49 logical JUnit cases, but native elapsed grows from 516.795s to
574.657s. The candidate contains a provider-kernel panic and a subsequent distinct successful
VM execution; all physical costs count. Known managed CPU and requested-CU execution exceed
their predeclared caps. The short Gateway budget also deliberately reduces default-duration
exposure. No candidate roster was promoted. The
[current evidence](common_backend_contracts.md#exact-source-slice-qualification) records source,
coverage, resource accounting and failure attribution separately.

The next implementation work targets repeated environment work and reliable execution. The
bounded teardown observer traced a full 30-second SIGTERM/SIGKILL delay to the branch's idle
native-client Pod. Its corrected PID 1 handles TERM; a real retained fixture exits promptly and
stack cleanup falls from 65.057s to 26.884s in one exposure. Three unchanged ordinary recovery
scenarios also pass with App-free fixtures, removing their App serving/client dependencies.
An attempted fourth migration fails an explicit App-stability assertion, so that History-outage
witness keeps its original App-backed fixture. These are coverage-preserving repairs and cuts,
not a qualified complete-feedback saving. The current native VM suite also passes on the existing
KVM worker in 226.691s with both harnesses and real KVM acceleration; this is
[feasibility evidence](vm_kvm_existing_pool_2026_11_12.md#current-native-fixture-14-november),
not a matched placement saving. A later fixed-source trial passes two VM and two Firecracker
actions, with real overlap on two distinct KVM workers. Existing autoscaling takes 188 seconds
from the additional worker's creation to readiness. This establishes concurrent execution,
not reliable cold/burst capacity, a fleet flake rate or a placement saving; whole-action physical
CPU/memory remain unknown. [Concurrent trial](vm_kvm_existing_pool_2026_11_12.md#concurrent-native-vm-and-firecracker-trial).

Rebasing exposes branch-only readiness code polling EndpointSlices and Leases removed by
devel. The branch now uses the stable Gateway Service and tests wrong-incarnation refusal
followed by correctly fenced Runtime status. Real Firecracker, managed VM and native Pod
contracts pass the focused validation; the final three direct guest targets and native-client
unit target also pass, while that invocation fails a type check subsequently corrected.
The attempted full-system KVM screen is canceled before its control arm rather than timed
through known source regressions. [Rebase evidence](common_backend_contracts.md#stable-gateway-rebase-qualification).

The next large experiment targets prepared disposable integration infrastructure and the
ordinary contract-owned gate. Warm retained Pod setup already takes only 27–34 seconds;
eliminating cold bootstrap by itself is not a 30% complete-feedback case. A useful substrate
must combine with shorter case tails or broader dependency cuts, preserve fresh identities
and mutable state, and beat the existing warm path as well as a fresh cluster.

## What has been tried

| Attempt                                                                | Observed outcome                                                                                                                                                                                                                             | Decision and remaining limit                                                                                                                                                                                                                                                     |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Real-process Runtime/Gateway/History recovery and lost admission reply | Recovery passes 25 fresh actions / 100 case exposures at an earlier source; seeded lost-journal/collision controls fail. A real Dispatcher retries after one committed admission reply is discarded and receives the same receipt.           | Working replacement-test building blocks. They do not prove controller/Kubernetes/guest equivalence or shorten selection by themselves. [Evidence](test_architecture_iteration4.md).                                                                                             |
| Packaged guest contracts using direct QEMU                             | Real kernel/root/Runtime, Gateway readiness, Claude/Codex two-turn Sessions, setup/fencing, CLI routes, credential revocation and private systemd proof pass, with omission/wrong-peer controls.                                             | Feasible ownership transfer. Keep real issuer/controller/KubeVirt/PVC/App and deployed OOM witnesses; qualification is incomplete. [Guest evidence](test_architecture_guest_session.md), [ownership](test_architecture_contract_ownership.md).                                   |
| Combined smaller Pod/VM/services graph                                 | Historical A/B/B/A: native mean **442.484s → 365.475s, 17.4% lower**; known CPU **5.4% higher**. Full VM baselines were cold; candidate VM baselines were warm.                                                                              | Useful feasibility screen, no causal complete-feedback claim. VM case work itself did not get faster. [Curated results](test_architecture_graph_screen_376.json).                                                                                                                |
| Actual affected-target selection                                       | First historical Runtime replay selects the same **51 tests** in both graphs; manual contracts do not replace ordinary tests. An MCP-only synthetic change drops exactly three unrelated deployment suites, **18 → 15** tests.               | Dependency cuts are real, but the full historical MCP frame had no isolated MCP-only PR. The fast ordinary Runtime gate has not been implemented or qualified. [Evidence](test_architecture_iteration4.md).                                                                      |
| Remove App/Runtime/MCP ownership and unused inputs from fixtures       | Lightweight service/App consumers lose unused Runtime/harness or MCP dependencies. Central recovery omits three unused Deployments; retained scenarios pass.                                                                                 | Several narrow ports are already in devel. Better invalidation and isolation; no quantified complete-PR saving. The held image split is not credited with a saving.                                                                                                              |
| Split Services and balance the 21 Pod cases                            | Manual slices retain their case sets and participate in the combined graph screen.                                                                                                                                                           | No matched 30% whole-graph result. More targets can duplicate setup and change queue/CPU exposure. [Progress](test_architecture_iteration3.md).                                                                                                                                  |
| Bridge four versus eight shards                                        | Same 24 cases; mean selected-target elapsed **24.9% lower**, worker CPU **62.8% higher**.                                                                                                                                                    | Branch candidate, not an established whole-CI or no-cost win. [Screen](../../app/debug/2026_11_13_bridge_sharding.md).                                                                                                                                                           |
| Precompile Bridge Python bytecode                                      | Warm full-body screen: **9.840s / 11.2% lower** selected-target elapsed, TestRunner CPU 14.2% lower. Earlier cold compilation cost 4,202.992 CPU seconds and added 83.444s.                                                                  | Not adopted; cache states differ and cold cost is material. [Evidence](../../app/debug/2026_11_13_bridge_bytecode.md).                                                                                                                                                           |
| Overlap independent harness-pair scenarios                             | Four cases can overlap without adding Sandboxes; first full-suite screen is mixed.                                                                                                                                                           | No demonstrated suite saving. Keep fresh case-owned state; simple concurrency was not enough. [Evidence](test_architecture_iteration3.md).                                                                                                                                       |
| Shorter reconnect-exhaustion fixture                                   | Five-second trial fails real recovery; 45-second candidate passes the unchanged recovery assertions versus a 120-second default.                                                                                                             | Explicit tradeoff: less default-duration exposure. Production/default recovery budgets remain; not a generic whole-CI win. [Evidence](test_architecture_iteration2.md).                                                                                                          |
| Faster Secret projection and a Pod annotation nudge                    | Annotation nudge did not improve recovery and was withdrawn. Five-second kubelet cadence gives 23–29s in three selected cases versus 22/29/91s control.                                                                                      | Branch experiment. Real same-Pod projection retained; cold baselines, small samples and cost remain unresolved. [Evidence](test_architecture_iteration3.md).                                                                                                                     |
| Overlap PodTemplate-only imports with readiness                        | One matching-runfiles pair: setup 140.131s → 125.428s; all 21 cases pass. Whole-action delta also includes case/import variation.                                                                                                            | Earlier bounded improvement, not a causal 63-second whole-gate saving. [Evidence](pod_template_image_import_overlap_2026_11_12.md).                                                                                                                                              |
| Skip already usable OCI import transactions                            | Same-worker warm A/B/B/A: setup mean **46.992s → 32.216s**, retaining exact digest/pin/unpack and clean-state checks. Real selected Pod/MCP/VM paths pass.                                                                                   | Usability logic is in devel; fleet/full-feedback benefit remains a follow-up. A cold baseline is not necessarily an empty image store. [Evidence](usable_import_transactions_2026_11_12.md).                                                                                     |
| Batch current Pod/tmpfs imports                                        | Earlier two-arm screen reverses its apparent saving with order. Isolated screen exposes a CoreDNS pull racing the cold-store check. Corrected clean pair: imports **0.606s faster**, physical action **4.225s slower**, CPU **9.1% higher**. | Stop this profile without a production batching port. Preserve the failed precondition, rather than classify it as an importer defect. [Evidence](current_import_batch_comparison.md).                                                                                           |
| Batch current VM/disk imports                                          | Four matching-input B/A/A/B actions: cold imports **15.1% lower**, physical import-test execution **18.0% lower**, CPU **16.1% lower**; all guards pass.                                                                                     | Promising smaller optimization. Production port, unchanged deployed VM and complete-graph comparison remain unperformed. Unmatched-root and invalid-command attempts are retained separately. [Full screen](current_import_batch_comparison.md#completed-disk-backed-vm-screen). |
| Smaller deployed guest root                                            | Codex-retaining root is **26.2% smaller**. One pair reduces physical execution only 11.086s / 4.0%, with worse readiness. A no-harness root cannot start embedded Execution.                                                                 | Keep manual; byte reduction is not proportional latency reduction. [Evidence](test_architecture_iteration7.md).                                                                                                                                                                  |
| Read-only guest image binds                                            | Both unchanged targets pass four-arm testing; candidate physical times/CPU are lower, but initial source warming differs.                                                                                                                    | Feasibility and removed copies, without a qualified matched complete-feedback saving. [Evidence](2026_11_13_guest_mounts.md).                                                                                                                                                    |
| Move private systemd proof into the existing direct guest              | All 11 real subtests and omission control work. Warm combined graph means **202.192s → 200.769s**, within readiness variation; initial control imports differ.                                                                               | Coverage composition works; no convincing speed win. Default deployed proof remains. [Evidence](test_architecture_iteration8.md), [comparison](test_architecture_iteration9.md).                                                                                                 |
| Run private proof in an earlier oneshot                                | Source audit shows it needs the actual Runtime systemd invocation, lifetime marker and ordered preparation.                                                                                                                                  | This arrangement is ruled out without an execution experiment. Other ownership-preserving overlap designs were not ruled out; its observed 22.51s is not a 30% lever. [Audit](vm_early_supervision.md).                                                                          |
| Share one Firecracker guest between trust and live-state cases         | Four-arm screen passes four actions / six cases. Queue/import exposure differs; two independent fresh guests become one compound fixture.                                                                                                    | No qualified saving; do not promote the changed isolation guarantee. [Experiment](test_architecture_firecracker_guest_reuse.md).                                                                                                                                                 |
| Batch seven fixture render profiles                                    | Existing assertions pass, but observed render window/action does not shorten and a VM-only consumer gains nine outputs.                                                                                                                      | Withdrawn. Lower aggregate CPU alone did not justify broader dependencies.                                                                                                                                                                                                       |
| Repair reliability defects found while measuring                       | Listener allocation, storage precondition and Quiesce repairs have focused proof. Quiesce reproduces unavailable routing then same-incarnation forwarding about **1.008s later**, avoiding the observed minute-delayed retry.                | These are useful repairs, not measured fleet flake-rate or complete-PR reductions. [Storage](storage_controller_startup.md), [Quiesce](firecracker_quiesce_retry.md).                                                                                                            |

The frozen graph comparison uses devel-equivalent required fixture defaults:
four Bridge shards, ordinary kubelet cadence, serial Pod pairs and the 120-second
Gateway reconnect deadline. The trials above remain historical evidence; their
savings and costs are excluded from the architecture-only comparison.

## Current graph screen and retained-tail experiments

The four fresh A/B/B/A exposures at `38f6c8e723` all pass: native medians are
412.876s for control and 291.913s for candidate, a 29.3% reduction. Each graph
has 40 labels and 46 physical test actions. The first control additionally ran
47 build actions; cache states and concurrent load differ. The 248 versus 224
JUnit rows do not prove equivalence across replaced targets. This synthetic
journal-only graph matches none of the 30 recorded historical PR change sets.
The policy remains shadow-only; neither the 30% screen nor the complete-PR
qualification passes. [Source and coverage limits](ordinary_contract_graph.md#frozen-graph-screen-and-applicability).

Further experiments use `38f6c8e723` plus documented working-tree overlays:

| Attempt                                      | Result                                                                                                                                                                                                                                             | Disposition                                                                                                                                                                                                                                                        |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Implementation plus owner regression tests   | The selector discovers same-package direct owner tests, keeps them required, and rejects shared, unknown and test-only replacement inputs. Real Bazel queries confirm the positive and negative cases.                                             | Useful eligibility repair; no broader ownership or speed claim.                                                                                                                                                                                                    |
| Real Quiesce/History seal/successor contract | [One focused case passes](https://app.buildbuddy.io/invocation/9c62fa6a-fa6b-4e24-b497-e06189e2842f): a routed command held across the durable seal is refused without mutation; a fresh Pod UID reopens at the next cursor and accepts new input. | Ordinary native coverage. The API server is real; controller decisions and credentials are fixture-owned. No controller/issuer replacement claim.                                                                                                                  |
| 45-second Gateway exhaustion budget          | The unchanged case passes in 228.8s, versus 243.4–271.6s recorded ordinary actions. Cache/source conditions are unmatched and the configuration changes.                                                                                           | Manual only. It shortens default-duration exposure; no causal saving or reliability claim.                                                                                                                                                                         |
| Three Dispatcher shards                      | All three original cases pass once; longest shard is 233.3s versus 218.0–246.8s ordinary. CPU grows to 799.3s from 301.4–388.6s, with three fresh stack setups.                                                                                    | Reject promotion: no convincing wall gain, substantially more compute.                                                                                                                                                                                             |
| Prepared Firecracker baseline                | Two full two-case runs pass. The same K3s process/plugin is reused with fresh workload identities; native 302.222s → 275.648s, 8.79% lower. Setup falls 48.375s → 21.772s; case work is nearly unchanged.                                          | Mechanism evidence only. One pair cannot qualify tails or flakes. The v1 shared data mount lacks cross-container ownership; adoption is blocked. A private-data v2 candidate remains unmeasured.                                                                   |
| Native Session driver                        | One real-process case passes: exact Session admission, setup output, a genuine History read timeout, recovery without accepting an earlier turn, and a third input. Its dependency graph has no App targets.                                       | The native VM/Firecracker migration is now implemented, with 14 caller builds passing; combined validation and physical acceptance are in progress. This is not yet a qualified speedup. [Evidence](common_backend_contracts.md#native-platform-driver-milestone). |
| Fresh private KVM data                       | The Docker-overlay placement fails during bootstrap. Moving only the fresh store to Bazel's action-owned ext4 workspace passes both ordinary Firecracker cases in one attempt; TestRunner 254.857s, Bazel 290.188s.                                | Working ownership prerequisite, not a matched speed comparison. Retained-state reuse, p90 and flakes remain unqualified; shared-store cleanup remains unexecuted. [Evidence](common_backend_contracts.md#private-fresh-kvm-prerequisite).                          |

The native platform migration now removes the App executable/image and HTTP testing client
from the required VM/Firecracker graphs. Exact rebased source `fc38753823` passes nine unit/render
targets and 36 explicit library/aspect checks. At `04cbff4776`, both original Firecracker cases
and all five migrated MCP Actions cases pass in one attempt. At `6c31cfb9f7`, the combined native
VM/Firecracker/Dispatcher, Session-driver and wire-client check passes all 11 cases in one attempt,
including the unchanged VM trace contract. Ordinary PR CI at that head passes all 2,005 test targets.
Its longest fresh target is Pod at 532.0s, versus VM 286.5s and Services 285.3s; these are one-run
observations, not a baseline/candidate saving. The
[current qualification record](common_backend_contracts.md#native-platform-dependency-migration)
also records the initial fixture/plugin, Actions mount and client-audience failures.

Source `f919e2ed22` adds a manual App-free four-case central Services candidate with the same
scenario assertions and Pod-bound fixture credentials. The original seven-case Services suite
remains required, including its three App/MCP witnesses. Its four cases, all seven original
Services cases, 21 render cases and four library/aspect checks pass in one focused invocation;
the dependency query excludes App serving/client/conftest inputs. Full CI catches the missing
new variant in the render-determinism expected set, now repaired without weakening byte equality.

The retested balanced Pod split at `6c31cfb9f7` retains the same 40 cases across the competing
graph and observes native Bazel 495.738s to 429.315s (13.4% lower in one B/A pair). Pod CPU is
35.8% higher; warmth, two extra candidate build actions and concurrent load differ. It is not
promoted. The first manual four-slot/eight-CU Pod candidate passes all 21 cases, but takes
442.477s versus the earlier control's 452.003s and exceeds the CPU/requested-compute screening
caps. It has cold setup of 130.737s versus 33.305s, while the case envelope falls from 321.105s
to 220.225s; this is an unmatched-source/warmth observation. Kubernetes reports 11 allocatable
CPUs, distinct from requested CUs. The subsequent shared-module six-slot configuration passes all
21 cases against a same-source ordinary control: 513.545s to 313.609s physical execution (38.9%
lower), 7.4% lower CPU, 22.1% higher requested-CU execution and 7.7% higher peak action memory.
Both infrastructures are warm; node capacity is intentionally different (seven versus eleven CPUs).
This isolated ordered configuration screen passes its resource caps. The same-source seven-target
comparison then passes the same 40 cases in both arms, reducing native Bazel elapsed from 489.497s
to 373.066s (23.8%). Known CPU is 1.0% lower, but requested-CU execution grows by 10.35%, failing
the predeclared 10% cap; peak concurrent requested CU grows from 28 to 32. Pod remains last at
330.949s, followed by Services at 291.709s. Keep the candidate manual. No complete required-feedback
median, p90 or flake result is established.
[Detailed evidence](common_backend_contracts.md#pod-partition-and-capacity-screen).

The [actual App-image consumer query](https://app.buildbuddy.io/invocation/ce224205-7707-4240-8c4b-9a5520a3b468)
leaves Pod and MCP Services among the four platform tails. This proves a dependency cut, not
ordinary-cache test avoidance or a latency reduction. The next implementation must make the smaller graph applicable to common real
backend changes with explicit assertion ownership. Do not expand the flat global
replacement list. Use owner-specific requirements and retained platform witnesses,
then measure the complete required-check path under ordinary cache conditions.

## Independent ports and validation state

Earlier fixture/import/profile/listener ports, storage precondition #16299,
selection archive #16316, Sampler TokenReview/History-outage coverage #16317,
Quiesce repair #16344 and route diagnostics #16363 are in devel. Their independent
value does not depend on adopting the architecture pivot.

[Notification fixture #16370](https://github.com/agentydragon/ducktape/pull/16370)
is merged in devel at `bb7c716f5a`. The architecture branch rebased onto that
source. [Native recovery #16374](https://github.com/agentydragon/ducktape/pull/16374)
was rebased to `34c305ccad` with focused and exact-head checks passing, then merged by the
repository owner on 14 November at 19:13:55 UTC as `2e4101b90a`. It adds ordinary coverage
without removing deployed witnesses.
Its initial failures were fixture setup; the repaired target includes a third
input after History recovery.

[Actions mount ordering #16386](https://github.com/agentydragon/ducktape/pull/16386) is independently
merged on 14 November at 17:22:30 UTC from `d34c757904`. The intended missing-settings-mount negative control fails;
the repaired target passes all 14 cases and generated manifests pass all 25 cases with no staging
snapshot drift. Its three required hosted checks pass. This fixes a real generator defect exposed
by omitting optional fixture secrets; no fleet reliability or latency benefit is measured.

Published main source `38f6c8e723` passes Bazel, pre-commit and artifact/import
checks, including ordinary BuildBuddy invocation `59ee9acc-a353-520b-8bb9-d8e1612771cc`. This full branch check is
not a representative routine-backend comparison. The current working milestone
adds an exact-owner shadow selector and promotes four contracts; deployed tests
remain required. The focused selector and native contract checks are recorded in
[the ordinary graph evidence](ordinary_contract_graph.md). The discarded-journal
control `6094b0b9` reaches the intended missing-retained-Session assertion after
real Runtime restart; its healthy counterpart is `254e237c`.

The [architecture report](https://agentydragon.github.io/ducktape/agentplane-test-architecture/)
was most recently published through merged report PR #16391 at gh-pages
`51dae409262bb1839caf876710258e52720450fd`. Iteration 16 has verified live HTML and evidence
hashes plus desktop/mobile light/dark browser checks. Its cutoff precedes the later Pod-only
and full-system composition screens above; those new measurements are not in that snapshot.

## Why a larger change is still needed

The diagnosed reference graph
[09528edd](https://app.buildbuddy.io/invocation/09528edd-001d-530e-bd0a-5a005d05d032)
tested merge `3691a3a18c` containing `87c576a69c`. It is a single source/cache
exposure, not a fleet baseline or a current branch/devel A/B:

| Required target | Physical execution | Inner action queue | Finish UTC   |
| --------------- | -----------------: | -----------------: | ------------ |
| VM              |           435.681s |             0.056s | 06:42:14.420 |
| Pod             |           358.477s |             0.059s | 06:40:56.667 |
| Services        |           308.945s |             0.059s | 06:40:09.085 |
| Firecracker     |           232.349s |            99.251s | 06:40:21.671 |

Native Bazel time is 578.721s; the GitHub Bazel job occupies 650s. These are
different clocks, and neither is a measured push-to-auto-merge distribution.
VM setup is 215.780s, including 190.477s across 25 import spans. Inclusive phases
overlap and cannot be added to each other or to prior savings.

Even deleting the VM action would expose Pod only 77.753s earlier: about 13.4%
of that native invocation, holding other clocks fixed. Removing Firecracker queue
alone would not shorten this graph. A 30% native reduction needs about 174s off
the last completion and improvement to the competing Pod/Services/Firecracker
tails. This is why a smaller image, more Bridge shards or one faster fixture is
not the remaining architectural answer.

Unfinished work includes an ordinary contract-owned gate, an end-to-end leased
or clone-backed integration substrate, matched whole-graph replays, burst/cold
exposure and reliability follow-ups. None has reached the adoption standard or
established diminishing returns. Raw artifacts remain private; reviewed results
and [tracker #16099](https://github.com/agentydragon/ducktape/issues/16099) retain
the measurement boundaries.
