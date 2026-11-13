# Fast CI through smaller real contracts

Tracker: [#16099](https://github.com/agentydragon/ducktape/issues/16099).
Experiment branch: `agent/test-architecture-pivot`.
Architecture milestones stay on [draft PR #16102](https://github.com/agentydragon/ducktape/pull/16102).
Independent bugfixes and clear improvements that already apply to devel may be
backported through separate PRs after validation.

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
  Iteration 3 moves the unchanged backend composition to
  `testing/native_service_fixtures.py`, with explicit pytest imports. The healthy
  recovery passes and discarded-journal control rejects the missing retained
  Session. Iteration 4 adds direct Runtime admission/deduplication across restart,
  active Runtime SIGKILL with interrupted-turn recovery and next input, and History
  process unavailability with retained-cursor replay. All four manual contracts
  passed in 25 fresh actions (100 case exposures, 25 per contract); these are not
  100 independent trials or low-flake-rate proof. Discarding the journal and
  disabling its collision guard both fail the intended assertions. A separate
  manual admission-reply-loss target now observes the actual Dispatcher retry:
  a test-only Runtime interceptor discards one successful admission response as
  `UNAVAILABLE`, then the real journal returns the same receipt. One submission
  causes two handler calls and one History admission; fresh next input completes.
  Rejecting a valid identical-command journal replay fails the contract. This models a post-handler
  error, not a raw socket reset. The existing Central Sampler native integration
  now uses real TokenReview with wrong-audience and unauthorized-account
  negatives that leave no session, then composes a real History process pause,
  completion into the Sampler journal, exact contiguous backfill and next input.
  It preserves its shell/patch/image, stale-incarnation and lease-release assertions.
  This is an envtest-backed process contract; deployed controller, identity and
  storage acceptance remains. The ownership inventory below identifies the
  complete retained witnesses before any gate promotion.
  Gateway/envtest/PostgreSQL and Sandbox fixtures are now reusable independently
  of History, harness/model fixtures and host Runtime startup. The existing
  recovery and admission-reply-loss targets pass after extraction. The configured
  guest uses that narrow plugin and an explicitly unused ingress endpoint; it
  creates no Session and starts no History service. Packaged guest Runtime and
  harness inputs remain required.
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
  Iteration 4 splits the unchanged seven services cases into manual 3-case MCP and
  4-case central-services targets. The latter retains the real app, Actions, PKI,
  controllers and backend, with no MCP edge or external OAuth fixture images. Its
  configured closure has no path to the MCP edge image. The current required
  seven-case target stays intact. Compare both slices together with unchanged Pod
  and VM suites; a quicker isolated slice alone cannot establish quicker PR feedback.
  A separate manual VM boot candidate exercises the real guest, issued SVID and
  authenticated app/Gateway/Runtime status. It does not replace the existing VM
  harness, systemd, cgroup, egress, notification or post-operation credential checks.
  The full existing VM and Firecracker fixtures also omit their unused MCP edge image/deployment;
  every retained rendered resource and scenario assertion is unchanged. Its full
  real VM scenario and both Firecracker cases pass; this proves retained behavior and graph selectivity,
  not a matched timing improvement.
  A separate app-free direct-boot experiment now starts the current kernel,
  initramfs, root and Runtime disk through pinned QEMU/virtiofsd on managed RBE.
  Its test-only systemd service proves PID1, cgroup v2, seven real mounts, a fresh
  nonce written through virtiofs and orderly poweroff. Empty configuration ISOs
  mean Runtime health and authenticated requests are outside this contract.
  Keep it manual; the wrong-config-serial control must fail guest readiness.
  This isolates generic guest packaging from K3s/KubeVirt acceptance without
  asserting that the broader deployment scenarios are redundant.
  A separate manual `test_runtime_gateway` now boots the unmodified fixture guest root
  and Runtime disk with fixture-issued TLS/configuration. Its healthy run
  passed authenticated Gateway status, running lifecycle and exact Sandbox/Pod/
  boot identity with a positive Lease epoch. The wrong-client-CA control reached
  the same readiness assertion and failed after a real TLS rejection. Both paths
  verified container removal, listener closure and restored state ownership.
  No Session or supervised executor is created. This adds a
  packaged Runtime transport witness; controller-issued credentials, KubeVirt,
  PVC, executor operations and App acceptance remain separate.
  Pod scenarios now have their own profile omitting only the MCP edge construct
  and image input. Actions remains required by command-box acceptance. All 21 Pod
  cases passed unchanged across source-pinned control/concurrency/profile runs;
  configured graph queries prove the image edge is absent. Unused app Connection
  routes remain unavailable in this profile, so external connections retain their
  separate MCP/app witnesses. Dispatcher/probe and default/MCP profiles retain their previous image ownership.
- **P4, compound scenarios:** separate idle authorization, failed-turn retention,
  timer, resume and identity contracts. Preserve the real >=60-second idle/resume
  acceptance and existing controller fake-clock matrix. Trace producer/watch timing
  for wrong-issuer to valid mounted Secret recovery before changing waits. Measure
  setup amortization, slowest action and CPU when regrouping cases.
  A Pod annotation spike did not improve the observed wrong-issuer recovery pair
  and was withdrawn. The retry-budget case now uses a separate 45-second
  reconnect deadline; ordinary production and the other recovery cases retain the
  120-second default. This trades default-duration exposure for a shorter real
  exhaustion test, without dropping unknown-effect, no-replay, session-recovery or
  next-input assertions. A five-second trial failed real post-roll recovery and
  remains recorded as a failed experiment, not a passing retry. The 45-second
  candidate passed the unchanged real recovery scenario and rendering checks.
  Four cases now overlap pre-reserved independent harness pairs without adding
  Sandboxes. Their first full-suite screen is mixed and does not establish a suite
  saving. The unchanged same-Pod Secret-recovery case varied13–109s; an experimental
  5s kubelet cadence produced 23–29s in three selected-case executions versus
  22/29/91s control. Real Secret projection and the >=60s idle timer remain. Node
  configz confirms both cadences; candidate baselines rebuilt cold, so repeat
  counterbalanced load/cost exposure before adoption claims. Live policy grant
  now also requires the original Pod UID.
- **P5, shadow/replay:** compose the candidate graph, replay >=30 pinned base/head
  pairs spanning Runtime/app/MCP/platform and three busy windows; collect >=100
  fresh contract executions. Include cold/warm and four-PR burst exposure. Pin both
  architecture trees and merge provenance, counterbalance order, preserve all
  failures/cancellations/retries, source/CAS identities, CPU and cleanup. Replay
  measures graph behavior; live PR shadows establish GitHub feedback clocks.
  Actual CI now archives the existing selected-label query with merge/base/head
  and invocation provenance, before runtime tag filtering, through its existing
  best-effort diagnostics. The real archive round trip is verified. Four selected
  graph launches preserve an automatic infrastructure re-execution and a VM
  fixture teardown error; this is neither 30 PR pairs nor a qualified speedup.
  The historical replay cohort now resolves 30 distinct tested base/head pairs
  from job-log source SHAs, matching BuildBuddy commit metadata and immutable
  merge parents. It retains failed/cancelled heads and all 40 workflow attempts.
  The first compatible historical replay, Runtime cleanup PR #15687, completed
  with the pinned d717 overlay and two audited pair-era BUILD/helper adaptations.
  Control selects 117 affected targets and candidate selects 124; both select the
  same 51 tests before runtime tag filtering. The seven extra candidate targets
  are image manifests and native fixture libraries. Manual contracts remain
  excluded, so this arrangement does not shorten Runtime PR selection. No selected
  tests ran in that replay; it proves neither timing nor coverage equivalence.
  The other 29 pairs remain unexecuted. Current PR API bases are recorded
  separately from tested bases.
  A separate synthetic MCP-edge-only selector probe at literal 91f compares full
  Pod/VM/Firecracker ownership with the candidate, holding architecture constant
  within each pair. Actual CI hashing selects 18 versus 15 tests; exactly those
  three unrelated suites disappear and all MCP acceptance stays selected. This
  proves the input cut, not a historical workload frequency or feedback saving.
  The full 15-head historical MCP frame contains no isolated MCP-edge-only PR.
  Required CI at d717 also queued its Firecracker action for 310.328s while all
  three registered compatible workers were occupied at the configured maximum.
  Removing only that queue has 153.499s of conditional test-tail headroom before
  VM becomes last. Compare narrower validated KVM ownership with bounded
  horizontal capacity under burst load; account for registration, cache,
  host/SSD eligibility and recurring reservations. An idle-floor change alone
  cannot remove this saturated-window wait; do not add VM traffic to that pool
  without capacity evidence. See the iteration 4 investigation for provenance.
- **P6, adoption:** make reviewable milestone commits on the existing branch/PR.
  Change a gate boundary only after the evidence below supports that boundary.
  Revert that selection change on an escaped contract or reliability regression.

P2, P3 and P4 diagnosis can proceed independently. P5 depends on their candidate
graph. Seven-day recurrence follow-ups in #16034 continue alongside this work.
Iteration evidence and the remaining coverage boundaries are recorded in
[`system_tests/debug/test_architecture_iteration2.md`](../system_tests/debug/test_architecture_iteration2.md).
The next implementation screen and its coverage/measurement limits are in
[`system_tests/debug/test_architecture_iteration3.md`](../system_tests/debug/test_architecture_iteration3.md).
Native recovery controls, MCP/VM image ownership and actual selection artifacts are in
[`system_tests/debug/test_architecture_iteration4.md`](../system_tests/debug/test_architecture_iteration4.md).

The case-level ownership index is in
[`test_architecture_contract_ownership.md`](../system_tests/debug/test_architecture_contract_ownership.md).
It distinguishes owner/native coverage from the real controller, issuer, App, storage
and guest assertions that remain deployed acceptance.

Four minutes is an intermediate budget, not a floor. After each measured change,
rank the next probes by likely complete-feedback or reliability benefit against
implementation, review, maintenance and developer disruption. Pursue candidates
with a credible payback within roughly one quarter. Stop dedicated optimization
when no such candidate remains and the two best remaining probes fail; preserve
their negative evidence and follow-up triggers rather than manufacturing more work.

## Admission evidence for a smaller gate

- Assign every unique assertion to a retained witness. Review historical fixes and
  seeded defects: lost journal, duplicate admission, stale incarnation, wrong issuer,
  History cursor gap, unauthorized delegation and browser reconnect. Negative
  controls reject the defect; healthy positive controls pass. The native cases alone do
  not establish the complete matrix or platform equivalence.
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

A seven-profile render batch with two full VM/Firecracker controls passed the
existing render assertions but did not shorten its observed render window or test
action. It also broadened a VM-only consumer to all nine outputs. The patch was
withdrawn; retain its aggregate CPU reduction as a revisit trigger only if renderer
capacity becomes a measured feedback bottleneck.
