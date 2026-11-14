# Agentplane CI architecture: experiment overview

Evidence reviewed on 14 November 2026 UTC. Implementation measurements below
identify their source; the latest import screen uses `6997d12620` on
[draft PR #16102](https://github.com/agentydragon/ducktape/pull/16102).
Optimization is paused. This write-up records completed work and outstanding
evidence; it does not promote a replacement gate.

**There is no qualified 30% reduction in complete PR feedback.** The branch has
working smaller contracts, useful independent ports, several rejected ideas and
a promising VM importer screen. The historical combined graph was 17.4% faster
on its native Bazel clock, with unequal baseline warmth and higher known CPU.
That is not a current branch-versus-devel or rerun-inclusive PR saving.

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

## Independent ports and validation state

Earlier fixture/import/profile/listener ports, storage precondition #16299,
selection archive #16316, Sampler TokenReview/History-outage coverage #16317,
Quiesce repair #16344 and route diagnostics #16363 are in devel. Their independent
value does not depend on adopting the architecture pivot.

[Notification fixture #16370](https://github.com/agentydragon/ducktape/pull/16370)
and [native recovery #16374](https://github.com/agentydragon/ducktape/pull/16374)
are open at this review. They add ordinary coverage without removing deployed
witnesses. Notification's two new tests have fresh passing JUnit evidence at
`2fd65524b0`; complete PR checks are still pending. Native port `605da061d5`
repairs an omitted test-launch parameter; preceding failures were fixture setup,
not scenario exposure. It also adds a third input after History recovery. Its
latest scenario/CI verdict was pending when optimization paused.

Main source `6997d12620` passed Bazel, pre-commit and artifact/import checks.
Earlier literal `87c576a69c` passed five fresh manual targets / eight nonempty
cases covering recovery, reply loss and packaged Gateway/Claude/Codex. Those
manual results belong to that earlier source, not every later rebase.

The gh-pages refinement is a separate unpublished snapshot. Its browser run
passed, but review found missing spaces around links; visual proof/publication
was not finalized before the pause. Do not treat it as the published latest result.

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
