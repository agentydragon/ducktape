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
  Apply the same ownership rule to App integration support: tests that author
  History retain the real App, Gateway, History and Kubernetes API fixtures,
  without inheriting an unused Runtime executable and harness closure. The
  bridge and history-gateway targets explicitly opt into the production Runtime
  composition and keep their restart, recovery and next-input assertions. Prove
  both removed and retained graph edges; removing an unused input is not evidence
  of a complete-feedback saving.
  Generic ASGI serving also no longer imports MCP support: the MCP-specific
  helper and existing endpoint case move to their owning test module. All 14
  focused ASGI/MCP cases pass, including a real client cancellation/retry. One
  CPython profile collects the same 24 Bridge cases with no MCP imports;
  collection-only runs are not contract exposures or complete-feedback timings.
  A source-pinned 4/8/8/4 Bridge probe passes the same 24 cases in all four runs.
  Eight shards lower mean selected-target elapsed by 24.9% while using 62.8% more
  measured worker CPU. The branch uses eight as a scheduling candidate; assess
  its mixed workload and busy-window cost before a devel backport.
  A full-body bytecode A/B/B/A passes all 24 Bridge cases in every arm, with 32
  NEW shard actions. Mean selected-target elapsed falls 9.840s and TestRunner CPU
  14.2%, but cache/action states differ and the earlier cold compile required
  4,202.992 CPU seconds. Keep bytecode collection unchanged until full-workload
  exposure justifies that cost; preserve the screen in
  `app/debug/2026_11_13_bridge_bytecode.md`.
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
  A separate manual `test_runtime_session` now drives the packaged Claude CLI
  through two real Shell turns, retaining a workspace nonce, original command IDs,
  contiguous sourced History and one Runtime boot. Its first turn also shares the
  VM shell probe for UID, PID1, the Claude service cgroup and systemd denial. The
  shared probe passes in a fresh direct Session, and the full deployed VM scenario
  passes with the extracted assertions. Both healthy targets retain separate
  guest-boot and authenticated-RPC readiness. The wrong-URI
  History control first proves a healthy service through a real mTLS Health.Check,
  then reaches the ordinary History assertion after Runtime rejects the SPIFFE URI.
  The fixture disables executor resource enforcement; the cgroup probe establishes
  placement only, not an executor budget. The target does not establish Codex,
  controller-issued credentials, KubeVirt, storage or App acceptance. Physical
  attempts, startup failures and the invalid first control are preserved in
  `system_tests/debug/test_architecture_guest_session.md`; this is feasibility,
  not an end-to-end CI saving or gate promotion. The latest full VM validation
  preserves a managed action-VM kernel panic and automatic retry in #16222.
  Read-only image binds replace eager copies in this manual launcher. A/B/B/A
  passes both unchanged targets in all four arms; candidate action times and CPU
  are lower, but first-arm input warming confounds transfer/command comparisons.
  Inputs and deployment coverage are unchanged. Details and limits are in
  `system_tests/debug/2026_11_13_guest_mounts.md`.
  The separate manual Codex guest contract now completes two real commands through
  the packaged VM executor, checking its actual memory/CPU limits and executor
  slice. Removing the executor fails the unchanged slice assertion. Gateway,
  Claude and Codex passed together in three fresh actions at the `2196229`
  milestone; actual OOM enforcement remains a deployed acceptance boundary.
  Session setup is a further obligation: fresh nested workspace and HOME ownership,
  process/aggregate/service budgets, privilege restrictions and detached-descendant
  fencing. Both direct guests and the full deployed VM passed with the same probe
  and assertions. A wrong-process-limit control fails the shared assertion after
  both original Shell turns. The deployed probe and the wrong-History control's
  original failure boundary remain intact. This proves assertion transfer, not a
  required-CI saving.
  Egress already has a real subprocess seam with caller-owned settings/API/CA;
  its own-SVID and credential-revocation case passes. The Notification subprocess
  driver passes with the caller's API/CA/database: it returns a seeded owned
  subscription and rejects an unrecorded trusted SVID. Same-workload credential
  revocation and real sidecar/packaged CLI composition remain before replacing
  deployed assertions. An empty subscription list is insufficient.
  Pod scenarios now have their own profile omitting only the MCP edge construct
  and image input. Actions remains required by command-box acceptance. All 21 Pod
  cases passed unchanged across source-pinned control/concurrency/profile runs;
  configured graph queries prove the image edge is absent. Unused app Connection
  routes remain unavailable in this profile, so external connections retain their
  separate MCP/app witnesses. Dispatcher and Session-deletion scenarios now use
  the same existing composition without MCP Edge; App, Actions, notification,
  controller and storage services remain. Deletion keeps its dedicated runner
  disk. Configured closure checks retain all 11 common service images and both
  required runner disks. Required CI at a69 passes both fresh deployment actions
  and all five unchanged cases; the input cut is not a latency measurement. Probe and default/MCP profiles
  retain their previous image ownership.
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

The next complete-graph screen combines both critical tails: the existing two
manual balanced Pod slices retain all 21 cases; a smaller deployed VM witness must
retain issued identity, real launcher/mount/relay configuration and post-operation
identity checks, while the packaged guest contracts own setup/tool/supervision
assertions. The current full VM stays required until that ownership and its
controls are proved. Run the candidate graph with all other selected required
tests, counterbalanced against the unchanged graph, before expanding to the full
30-pair cohort. Measure the last gate and added setup/CPU, not a sum of isolated
contract gains. At the `bedb9a5` checkpoint Pod ended only 9.412 seconds after VM;
improving only one cannot establish a large full-feedback saving.

The later 542e615 checkpoint has 13 K3s target summaries, with Pod finishing
51.265 seconds after VM. This is still a two-tail problem; these summary clocks
do not cover the complete gate or qualify physical execution. The next guest
composition adds actual CLI routes through Egress/sidecar/Notification and
same-credential revocation, while the deployed candidate retains both real
guest boots, issued identity, private supervision, workspace work and unchanged
trace assertions. Both packaged guest compositions pass after repairing the
fixture's nested Go test-wrapper lifetime. The deployed candidate also passes
Egress Identity and Notification CLI checks inside its existing tool turn. The
wrong Notification peer-URI control fails the owned-data contract. These are
focused positive/negative proofs, without a matched complete-CI saving. Their
implementation failures and remaining controls are in
[`test_architecture_iteration6.md`](../system_tests/debug/test_architecture_iteration6.md).

P2, P3 and P4 diagnosis can proceed independently. P5 depends on their candidate
graph. Seven-day recurrence follow-ups in #16034 continue alongside this work.

The first historical-population A/B/B/A screen at `376f8ae` now passes all four
arms: 562 fresh physical test launches, exact retained Pod/unchanged-case
identities and no failures, errors, skips or retries. Native invocation elapsed
averages 442.484s for the full Pod/VM composition and 365.475s for the candidate
(17.4% lower), with 5.4% more reported CPU and one unknown CPU record per arm.
This is not complete required feedback or a branch-versus-devel saving.
Importantly, the smaller VM body does not shorten measured case work: 171.775s
full versus 173.669s candidate. Both full baselines were cold while both candidate
VM baselines were warm, dominating the apparent VM process reduction. Do not
promote that witness as a speed optimization.

The next architecture spike separates a single real deployed VM platform witness
from duplicate model turns. It must retain issuer/Pod binding, authenticated
Runtime readiness, workspace execution and real CLI routing; both packaged guest
contracts retain harness setup/fence, two-turn and History ownership. Private
kernel, launcher, mounts and supervision need explicit ownership and seeded
controls before any gate changes. The deployed witness and MCP Services nearly
tie in the warm candidate, with Pod next; measure the combined graph rather than
extrapolating an isolated improvement. The existing importer draft #15126 remains
unproven and should be revisited only through a repaired matched benchmark.
See [`test_architecture_iteration7.md`](../system_tests/debug/test_architecture_iteration7.md)
for the complete screen, cache limits and next experiment.

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
