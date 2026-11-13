# Fast CI through smaller real contracts

Tracker: [#16099](https://github.com/agentydragon/ducktape/issues/16099).
Experiment branch: `agent/test-architecture-pivot`.

Reduce the physical work and dependency graph needed to establish each guarantee.
Keep the current required gate while the replacement graph is proved. Desired routine
backend feedback is mean <4 minutes and p90 <8 minutes, including reruns and all
required checks. These are targets, not measured improvements.

## Candidate architecture

1. Owner tests cover state machines, authorization rules and injected-clock timing.
2. Real service contracts compose production binaries, real databases, native gRPC
   and mTLS. Runtime keeps its PID1 container. Script external model/provider behavior
   explicitly; never describe a fake service as the real boundary.
3. Focused deployment acceptance owns packaged images, controllers, PKI, RBAC,
   Secret projection, scheduling, storage and Pod/VM/Firecracker lifecycle.
4. App, browser and MCP journeys retain actual client, login, projection, approval
   and routing guarantees. Direct gRPC does not replace an app-owned contract.

Each target declares the binaries, images and artifacts needed by its guarantees.
The existing `bazel-diff` selector follows those dependencies; do not add a blind
path filter. Unknown mappings retain the broad test. Infrastructure/schema/auth
changes select their affected real deployment contracts before merge.

Fresh case-owned databases, Sandbox UIDs, identities, credentials, Runtime homes
and journals isolate state. Only an intentional in-case restart retains its journal.
Read-only artifacts and certified infrastructure baselines may be reused under
explicit compatibility keys. Setup failure and injected-fault cleanup have owners.

## Work lanes

- **P1, recovery pilot:** `//agentplane/x/native_contracts:test_recovery` composes
  production Gateway and History processes, Runtime PID1 and PostgreSQL containers,
  envtest API and scripted model. Exercise first turn, real listener outage,
  independent retained History, same-journal restart, Dispatcher receipt replay and
  fresh input with two admissions/completions. Inspect the actual failure of an
  opt-in discarded-journal control. Keep it manual during the spike.
- **P2, reusable contract fixtures:** remove app ownership from shared backend test
  support, then add independently reviewable Gateway reconnect/fencing, Runtime
  journal, History outage/cursor and authority contracts. Avoid the inherited
  `system_tests/conftest` app dependency. Start with named callers rather than a
  general fixture framework.
  The reused launch helper now leaves CLI artifacts to its callers. The Claude-only
  pilot retains Claude and has zero Codex input paths (previously 86); the Codex
  companion declares its own bundle. These counts are not transferred bytes.
  Representative Claude/Codex consumers and the discarded-journal control were
  exercised; this does not finish reusable fixture extraction or the contract matrix.
- **P3, deployment slices:** replace the common 12-service-image map applied to
  all 13 ordinary CI-eligible K3s suites with explicit backend/app/MCP/platform
  compositions. Inventory each unique assertion before removing a dependency.
  Inspect the configured closure and run retained real packaged-image acceptance.
  The branch now selects a central recovery profile for MCP Gateway crash,
  retry-budget, sampler-roll and History-outage targets. All four fault contracts
  remain separate with unchanged scenario assertions. The rendered closure omits
  only Dex, MCP Everything and the OAuth fixture: 175 to 162 objects and 15 to 12
  Deployments. Real packaged services, Kubernetes faults and Pod-bound client
  credentials remain. Other MCP/OAuth and app/browser targets keep their profiles.
  Broader per-contract ownership of the common service image graph remains open.
- **P4, compound scenarios:** separate idle authorization, failed-turn retention,
  timer, resume and identity contracts. Preserve the real >=60-second idle/resume
  acceptance and existing controller fake-clock matrix. Trace producer/watch timing
  for wrong-issuer to valid mounted Secret recovery before changing waits. Measure
  setup amortization, slowest action and CPU when regrouping cases.
  A Pod annotation spike did not improve the observed wrong-issuer recovery pair
  and was withdrawn. The retry-budget case now uses a separate 45-second
  reconnect deadline; ordinary production and the other recovery cases retain the
  120-second default. This trades default-duration exposure for a shorter real
  exhaustion test, without dropping unknown-effect, no-replay, retained-session or
  next-input assertions. A five-second trial failed real post-roll recovery and
  remains recorded as a failed experiment, not a passing retry. The 45-second
  candidate passed the unchanged real recovery scenario and rendering checks.
- **P5, shadow/replay:** compose the candidate graph, replay >=30 pinned base/head
  pairs spanning Runtime/app/MCP/platform and three busy windows; collect >=100
  fresh contract executions. Include cold/warm and four-PR burst exposure. Pin both
  architecture trees and merge provenance, counterbalance order, preserve all
  failures/cancellations/retries, source/CAS identities, CPU and cleanup. Replay
  measures graph behavior; live PR shadows establish GitHub feedback clocks.
- **P6, adoption:** change one gate boundary per PR after the evidence below.
  Revert that selection change on an escaped contract or reliability regression.

P2, P3 and P4 diagnosis can proceed independently. P5 depends on their candidate
graph. Seven-day recurrence follow-ups in #16034 continue alongside this work.
Iteration evidence and the remaining coverage boundaries are recorded in
[`system_tests/debug/test_architecture_iteration2.md`](../system_tests/debug/test_architecture_iteration2.md).

## Admission evidence for a smaller gate

- Assign every unique assertion to a retained witness. Review historical fixes and
  seeded defects: lost journal, duplicate admission, stale incarnation, wrong issuer,
  History cursor gap, unauthorized delegation and browser reconnect. Negative
  controls reject the defect; healthy positive controls pass. The pilot alone does
  not establish this matrix or isolated Runtime deduplication.
- Initial speed screen: >=25% lower median complete required feedback with p90 no
  more than 5% worse across the matched sample. Include pre-commit and artifact/import
  checks, rerun-inclusive time and failed/cancelled heads; do not infer savings by
  subtracting one small pilot from a broader suite or summing overlapping spans.
- Report physical failure exposure, result-stream false reds, cached results,
  signatures, denominators and intervals separately. No new unexplained failure or
  cleanup signature. Zero failures in 100 runs still permits about a 3% one-sided
  95% upper failure bound; repeatability screening is not proof of a low flake rate.
- Unknown coverage, vacuous controls, state leaks or unmatched capacity/cache
  exposure stop promotion. Preserve the current gate until those gaps are resolved.

The pilot authors controller-produced status in envtest. It cannot establish real
controller reconciliation, scheduling, Secret projection, service discovery, image
packaging, network policy, VM boot or app behavior. Those acceptance paths remain.

The existing image split #15214 stays held pending its own saving. Import usability
#16028 is a separate promising setup optimization. Earlier KVM, MCP overlap and
harness-mount experiments establish no complete-CI win and do not rule out different
graphs or adequately provisioned capacity. Retained infrastructure and Pod runner
pooling already exist; a parked shared-cluster spike is not a working prerequisite.
