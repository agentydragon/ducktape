# Fast CI through smaller real contracts

Tracker: [#16099](https://github.com/agentydragon/ducktape/issues/16099).
Experiment branch: `agent/test-architecture-pivot`.
Architecture milestones stay on [draft PR #16102](https://github.com/agentydragon/ducktape/pull/16102).
The broader optimization work is paused. Branch cleanup and maintenance continue;
the work below is a resumption plan, not authorization to launch more experiments.

## Goal and present boundary

Make routine Agentplane backend PRs reach trustworthy green at least 30% faster
in median complete feedback, including reruns, with no worse p90 or flake rate.
Preserve important behavior, security and platform coverage; document resource
and maintenance cost. Keep the PR draft until its gate changes are qualified.

The [experiment index](../system_tests/debug/ci_architecture_experiment_overview.md)
is the canonical status/results inventory. Detailed notes preserve failed
attempts, negative controls, cache/resource limits and immutable invocation/source
links. The [pre-cleanup plan](https://github.com/agentydragon/ducktape/blob/7114637397306a4883eeacedf0bb0a7e20817179/agentplane/plans/fast_ci.md)
retains the former chronological log; it is historical, not the current work queue.

The branch implements the [ordinary partitioned graph](../system_tests/debug/ordinary_partition_graph.md):
disjoint App/native Pod and MCP/native Services cases, unchanged complete VM
acceptance on KVM, and unchanged Firecracker cases without incidental Notification
setup. Devel's newer delegated-Action case remains on its full App-backed fixture.
Ordinary Bazel dependencies select tests. No source directory exemption is added;
the old journal substitution selector stays shadow-only. The full Gateway budget
and App-dependent History-outage witness remain.

Two fixed-source 50-case system-subtree comparisons are 35.4% and 35.2% lower,
with matching cases and passing declared resource proxies. They do not qualify
representative whole-PR median/p90, flakiness or actual KVM cost. Earlier negative
compositions and the additional current Action case remain in the evidence record.
No qualified 30% complete-feedback improvement has been established.

## Architecture and coverage contract

1. Owner tests cover state machines, authorization and injected-clock timing.
2. Real service contracts compose production binaries, databases, native gRPC
   and mTLS. Runtime keeps PID 1. Model/provider behavior is explicitly scripted.
3. Deployment acceptance retains packaged images, controllers, PKI, RBAC, Secret
   projection, scheduling, storage and Pod/VM/Firecracker lifecycle guarantees.
4. App/browser/MCP journeys retain actual client, login, projection, approval and
   routing guarantees. Direct gRPC does not replace App-owned behavior.

Each target declares the binaries/images/artifacts needed by its guarantees.
Bazel-diff follows those dependencies; unknown ownership and graph-wide changes
retain broad fallback. Implementation-plus-owner-test changes retain their owner
tests. Shared, test-only, BUILD, protocol, auth and packaging inputs never gain
replacement eligibility merely from their path. Preserve the known package-default
fingerprint counterexample during selection qualification.

Cases own fresh databases, namespaces, Sandbox UIDs, identities, credentials,
Runtime homes and journals. Only an intentional restart retains its journal.
Read-only artifacts or certified infrastructure may be shared with explicit
compatibility and isolation. Setup failures and injected-fault cleanup have owners.
The [assertion inventory](../system_tests/debug/test_architecture_contract_ownership.md)
records the deployed witnesses that direct/native tests do not replace.

## On resumption: qualification first

1. **Replay actual selected graphs.** Use the pinned 30 distinct tested base/head
   pairs and all 40 workflow attempts, retaining failed/cancelled heads. Run source,
   regression-test, BUILD and mixed-input selection on control/candidate trees;
   archive actual submitted leaf labels after tag filtering. Count broad fallbacks
   in the denominator. The old Runtime replay selected the same 51 tests and the
   journal policy matched 0/30 changes; neither demonstrates broad applicability.
2. **Run the complete required gate.** Counterbalance control/candidate order at
   fixed sources, retain current binaries, and include unaffected required tests,
   pre-commit and artifact/import checks. Compare normal caching and all reruns,
   not isolated target times or sums of overlapping phase spans.
3. **Exercise reliability and capacity.** Include at least three busy windows,
   cold/warm and four-PR bursts, and at least 100 fresh contract executions.
   Preserve physical failures, provider retries, cancellations, result-stream
   false reds, cleanup and signatures separately. A zero-failure 100-run screen
   still permits roughly a 3% one-sided 95% upper failure bound.
4. **Review resources and admission.** Account for managed and KVM physical CPU,
   concurrent memory/disk and recurring reservations. Requested CUs are proxies.
   For the next architecture screen predeclare at most 10% extra total CPU and
   CU-min and 20% extra peak requested CUs; disclose unknowns and any changed caps.
   There is no unmeasured persistent-infrastructure allowance.

Promotion requires at least 30% lower median complete feedback, no worse p90 or
flake rate, retained assertion ownership and effective seeded-defect controls.
Unknown coverage, state leaks, vacuous controls, unexplained failures or unmatched
capacity/cache exposure stop promotion. Do not expand the flat replacement list
to manufacture eligible PRs. Revert a promoted selection boundary on an escaped
contract or reliability regression.

## Larger remaining levers, if qualification exposes a gap

**KVM admission and concurrency.** In pinned ordinary CI `7246b046`, Firecracker
waits 144.641s and VM 94.300s while all three compatible workers are occupied at
the configured maximum. The last managed test bounds eliminating all KVM wait
to about 120s in that run, holding other work fixed. Test eligible added capacity
or measured per-worker concurrency, isolation and actual resource use. Faster
cold scale-up is a separate problem: 120s stabilization, at most +1 worker per
15 minutes, and an observed 188s creation-to-ready interval. An idle-floor change
cannot fix a saturated pool. Do not add traffic without busy/burst evidence.

**Prepared disposable integration infrastructure.** Compare compatible disk
clones with leased prepared infrastructure. Every lease keeps fresh workload
state/credentials, cross-lease denial, fault containment, dirty-state rejection
and cleanup. Cluster-wide faults require exclusive disposable leases. The full
end-to-end substrate has not been exercised. Shared-data v1 has an ownership gap;
fresh action-private ext4 storage passes but does not prove retained-state reuse.
PKI-only preparation lacks measured headroom, and a stopped hosted seed adds
restart work to an already warm path. First prove two simultaneous isolated
leases on unchanged real contracts; then test full feedback and recurring idle
cost before expanding. No silent reduction of fresh-cluster coverage.

**Broader owner-specific gates.** If ordinary dependencies still overselect,
map each removed deployment assertion to a real retained owner before changing
selection. The App handler class was only 3/46 sampled backend PRs and journal-only
eligibility was 0/30. Qualify broad real applicability, including BUILD edits,
rather than narrowing the goal to an eligible-only population. Retain real
controller/issuer/KubeVirt/PVC/App and Firecracker checkpoint witnesses.

These are conditional 30% hypotheses, not additive savings. Import batching,
smaller roots, Bridge shards and private-fixture relocation remain component
options with bounded or confounded results; see the index before repeating them.
The held image split #15214 still needs its own demonstrated saving.

## Economic stopping rule

Four minutes is an intermediate budget, not a floor. After each measured change,
rank the next probes by likely complete-feedback/reliability benefit against
implementation, review, maintenance and developer disruption. Pursue credible
payback within roughly one quarter. Stop dedicated optimization when no such
candidate remains and the two best remaining probes fail; retain negative evidence
and explicit revisit triggers. The current evidence has not established that point.
Keep subsequent architecture work on this branch/PR. Independent, clearly useful
repairs may be ported after validation; merged ports belong in the experiment index,
not in this plan's remaining work queue.
