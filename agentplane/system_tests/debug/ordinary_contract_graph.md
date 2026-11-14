# Ordinary backend contract graph

The next candidate replaces repeated deployment acceptance for implementation
changes whose behavior has a real process or packaged guest owner. It is not
qualified for adoption. `ci_contracts.json` is in `shadow` mode: ordinary CI keeps
all affected deployed tests and archives the proposed graph with its source
provenance. The four replacement contracts run as ordinary tests, not manual
experiments.

The first coverage domain is the exact source ownership of the Runtime journal.
Bazel resolves its actual `srcs`; a directory name does not grant eligibility. Every changed
file must belong to the declared implementation sources or their directly
dependent ordinary owner tests in the same package, and every direct rule consuming
those files must be accounted for. Mixed changes, BUILD files,
protocols, auth, packaging, manifests and unknown owners keep broad acceptance.
Policy changes themselves force the full sweep. This first domain does not finish
the requested routine backend graph: History, Gateway, MCP and App ownership still
need their own assertion mapping and representative selection evidence.

For eligible changes the candidate replaces the Pod, VM, Services and Firecracker
targets with real Runtime recovery/admission and packaged Claude/Codex Session
contracts. A source audit supports the VM substitution: both direct guests use
the current kernel/root/Runtime package, systemd, real pinned harnesses, Gateway,
Dispatcher and History, including shell write/read, setup/fencing, cgroups,
platform CLI routes, credential revocation and retained ordered History. Native
contracts add restart, crash, reply loss and History unavailability. Deployed VM
ownership remains actual controllers, cert-manager, per-Pod credentials,
KubeVirt and PVC/network wiring. The other three substitutions require the same
assertion-by-assertion audit. That audit rejects extending the current substitution
to whole harness, stream or Session/lifecycle libraries. The source audit identified three seams needing dedicated witnesses: a harness
model call through the real Egress proxy; a real Gateway roll during a live
central read/turn; and Firecracker process continuity across checkpoint/thaw.
Both packaged guest harnesses now send their actual model traffic through the
real sidecar/Egress route to an mTLS scripted endpoint. The tests require an
allowed CONNECT decision for that Sandbox Identity. The native Gateway-roll contract now passes with a bounded resume repair.
Its initial real-process outage exposed a structured control-route refusal that
abandoned a live read; the fix retries only the not-dispatched retained-session
initialize handshake within its existing deadline. Starts and writes keep their
no-replay behavior. Keep the matching deployed cases for the
remaining surfaces; Firecracker checkpoint coverage requires the actual platform. This scoped starting policy
does not narrow the overall goal to journal-only PRs.

The CI driver now submits a separate test-label file while retaining every
affected build target. Materializing test-suite leaves explicitly filters manual
and live API tags. A replacement must resolve to an ordinary runnable test;
missing/manual replacements fail instead of producing a coverage gap. Eligible
required contracts cannot succeed via Bazel's no-tests exit code. Shadow records
contain eligibility, accounted owners, replaced labels and the full candidate
test set alongside the existing merge/base/head identity. The archive also records
the actual submitted leaf labels after the CI tag filter, independently of the
pre-filter selection and shadow proposal.

Next, validate ownership queries and selected labels against the actual Bazel
graph, including protocol/platform counterexamples. Run every selected candidate
test alongside all unaffected required tests, with matched baseline inputs.
Qualify complete required-check feedback, all failures/reruns, p90, flake rate,
CPU, memory, disk, cleanup and cold/busy/burst behavior. The first complete screen
must establish a credible path to 30% before expanding the cohort. Until that
evidence exists, `required` mode is an experimental execution option, not the
ordinary acceptance policy.

The added ownership queries and contract processes have recurring cost. Removing
duplicated deployment work is the expected compensation, still unmeasured. There
is no persistent infrastructure or idle reservation in this design. Maintenance
consists of reviewing explicit ownership when contracts move, maintaining seeded
defect controls and defaulting unknown source consumers to broad acceptance.

## First source/query checks

At `7efbf06932c0b90a3c8bb610e79bbfaeb74e34db` plus overlay artifact
`8ee594d08767c146a83ae5cdb058f44dcd86967ef79eaf3a4bfe8947ea722d5d`, a real
Bazel reverse-dependency query for `journal.go` returns 45 test labels. The
candidate removes exactly the four declared deployment targets, retains 41
labels before CI tags, and materializes 40 runnable candidates. The input 45
already includes the four promoted contracts and one live API test. An honest
control therefore has 40 runnable labels after excluding those five; comparing
against the larger shadow graph would inflate the baseline.

The same selector rejects stipulated Session, protocol, BUILD and mixed
journal/protocol source changes and leaves its input test set unchanged. These
are real ownership/tag queries and classifier counterexamples, not a replay of
actual PR hashing or a timed test execution. Shadow mode changes none of the
ordinary submitted tests. The first query probe rejected every replacement
because of regex backslash interpretation; the corrected boundary pattern passes
the real query instead of relying only on the query test double.

The focused CI-driver checks pass in
[f973ff88](https://app.buildbuddy.io/invocation/f973ff88-892c-4354-8ed7-734f2d18ceaa):
40 JUnit cases with zero failures, errors or skips. These include no ordinary
affected targets with a required contract and rejection of a no-tests exit from
that required contract; the archive verifies actual submitted labels separately
from pre-filter labels.
The strengthened recovery and admission contracts pass in
[254e237c](https://app.buildbuddy.io/invocation/254e237c-d03e-4931-88e6-66fdcf5f0970),
including a third input after History recovery and an unchanged retained prefix.
Both invocations use working-tree overlays on the source above. They are focused
validation, not whole-PR latency, p90 or flake-rate evidence.

The seeded discarded-journal run
[6094b0b9](https://app.buildbuddy.io/invocation/6094b0b9-592f-4152-8244-f6a418b24e8c)
fails at the intended retained-Session assertion after real Runtime restart. It
does not fail during fixture setup. The healthy counterpart above retains that
Session and accepts the next input. This establishes sensitivity to the missing
journal in this arrangement; it is not a measured production flake rate.

The real-model Egress path passes for
[Claude](https://app.buildbuddy.io/invocation/2a8ac8fb-c054-469d-9a46-e4fd68224a1f)
and [Codex](https://app.buildbuddy.io/invocation/171d8748-2bad-4024-b294-95edc39cdc61).
The Claude run precedes final formatting and the Codex BUILD addition; the Codex
run uses those final guest changes. These are current-working-tree focused
passes, not a wrong-route defect control, real issuer/controller validation or
a complete graph result.

Before the graph comparison, the required fixtures return to devel defaults:
four Bridge shards, ordinary kubelet sync cadence, serial Pod harness pairing,
and the 120-second Gateway reconnect deadline. The earlier larger shard count,
shorter cadence/budget and overlap trials remain documented experiments. Their
separate effects and resource costs must not be folded into a graph-only result.

The Gateway-roll contract initially failed with real lost progress in
[a506292e](https://app.buildbuddy.io/invocation/a506292e-853e-422f-a0a7-6331b71611d8).
After the narrow resume repair, the
[executor owner tests](https://app.buildbuddy.io/invocation/14028ff2-3586-440c-89f8-82880c544d83)
and [real Gateway integration](https://app.buildbuddy.io/invocation/a392bf47-fdc5-4cf9-be9b-410175fdff30)
pass. The latter retains the same process and cursor, one process start, exact
output once, turn completion and a fresh input after recovery. These are
working-tree proofs; no representative rerun-rate improvement is established.

An additional real-process Runtime contract now covers the admission/Quiesce
cutoff in `agentplane/x/native_contracts/test_quiesce_cutoff.py`. It holds an
authenticated interrupt after real Gateway routing but before the production
handler, waits for real Controller Quiesce and the History seal acknowledgement,
then releases it and requires `FAILED_PRECONDITION`, byte-identical sealed
History/feed state and no admitted delayed command. It also replaces the Pod,
checks Gateway `GetStatus` against the expected successor UID and Runtime boot
ID, verifies the retained native Session is dormant without a process, and
submits fresh Dispatcher input. The test requires one `journal_reopened` at the
next cursor under the successor UID, the exact old History prefix, no replayed
interrupt, and successful completion.

The first successor check was not identity-fenced and failed with Gateway
`UNAVAILABLE` (`a2bf4397` and `c82a92c5`). The fixture now labels the Pod for
Gateway discovery and waits for the exact new UID and boot ID. That exposed a
test assumption rather than a production failure: fenced status correctly
reported `DORMANT` before new demand (`587e2e6e`). `session/status.go` derives
that state when no process or pending work exists; successor adoption in
`internal/coordinator/protocol.go` reopens and resumes retained Sessions, while
`system_tests/scenarios/suspend_resume.py` likewise asserts dormant inventory
before the next input. The contract now follows that order and passes in
[9c62fa6a](https://app.buildbuddy.io/invocation/9c62fa6a-fa6b-4e24-b497-e06189e2842f).

The Runtime, Gateway, History and Dispatcher are real processes. This is not a
Kubernetes-controller, certificate-issuer or credential-rotation witness:
`SandboxCluster.replace_runner_pod` authors Pod readiness, Sandbox incarnation,
WorkloadCredential binding and accepted-binding status in envtest. The
controller token is distinct and exercises the production Runtime authorization
boundary for Quiesce. This added case is not in the frozen graph comparison or
qualified replacement policy, and makes no performance claim.

The first frozen timing controller stopped before any arm: source `5baec60b89`
failed a helper-library mypy aspect even though the focused guest targets passed.
The Egress client field used the synchronous generated stub type for an async
channel; its annotation is corrected to `EgressAdminAsyncStub`. The retired
budget profile also required removing its expected output from the determinism
case; that correction passes in
[8fe254eb](https://app.buildbuddy.io/invocation/8fe254eb-a62b-44d3-a820-0ab90dd1896a).
Both failed CI attempts remain part of this branch's validation history. Neither
attempt started a 40-vs-40 timing arm.

Explicit builds of the changed Gateway fixture, Sampler store and Egress helper
libraries pass their lint/type aspects in
[f757a24e](https://app.buildbuddy.io/invocation/f757a24e-dcc2-4b3f-8f23-21cfc99c7070).
Focused py_test success alone did not check those helper-library aspects.

Source `0279cfc028` also stopped before any timing arm. Its
[full CI run](https://app.buildbuddy.io/invocation/07065d23-3c61-5ef8-9f7c-0886f1c6d394)
ran 1,999 test targets; only `app:test_history_gateway` failed, in both harnesses'
permanent Gateway-stop cases. Retaining the parent's listener for restart left
a listening socket without a server after `stop()`, changing connection refusal
into a deadline expiry. Permanent stop now closes the parent socket; restart
continues to retain it. The unchanged six App cases and real Sampler Gateway-roll
case pass together, with the fixture library's aspects, in
[2c2e1bd7](https://app.buildbuddy.io/invocation/2c2e1bd7-2421-4f6a-988f-9fa3fd631c2f).
This is a fixture regression repair, not a measured production reliability gain.

## Frozen graph screen and applicability

All four frozen `38f6c8e723` arms pass with fresh results, one run/attempt and 46
distinct physical TestRunner actions each:

| Order                                                                           | Graph     | Native Bazel seconds | Emitted JUnit cases |
| ------------------------------------------------------------------------------- | --------- | -------------------: | ------------------: |
| [A1](https://app.buildbuddy.io/invocation/eb687805-7873-47bc-9d82-73a8a0be545a) | baseline  |              389.333 |                 248 |
| [B1](https://app.buildbuddy.io/invocation/f90bbec4-5052-4158-9b66-93292513df7a) | candidate |              317.275 |                 224 |
| [B2](https://app.buildbuddy.io/invocation/09de2c2b-5d4e-42f7-80c7-f6f42e7ad7a0) | candidate |              266.551 |                 224 |
| [A2](https://app.buildbuddy.io/invocation/9415c9cc-3086-4e6a-b4ba-8a46093dc1ce) | baseline  |              436.418 |                 248 |

The two-observation medians are 412.876 and 291.913 seconds, a 29.3% reduction.
This misses the 30% screening threshold and is not complete required-check
feedback, ordinary-cache PR behavior, p90 or a flake-rate measurement. Target
selection was queried at `4bf33d0e350`; subsequent changes before execution
changed no Bazel metadata or selector. A1 additionally executed 47 non-test build
actions; the other arms executed only their 46 test actions. Cache state is
therefore unequal, and separately running focused checks are load confounders.

Both graphs contain 40 labels, with four deployed targets replaced by four
native/direct contracts. Of the 36 common labels, all 33 with testcase identities
have matching identity sets. Three common Go targets have successful XML without
testcase rows. The different case totals and equal target counts establish no
semantic equivalence across replaced targets; those assertions still need
explicit owners. Test-resource estimates are within the predeclared screening
ceilings. Known CPU sums fall, but one baseline action per arm has unknown CPU;
disk use is unknown, and peak requested CU is not available executor capacity.

None of the 30 prepared historical PR change sets is journal-only. Their paths
have not been silently remapped across later source moves. The current policy
cannot establish a speedup for that cohort and remains in shadow mode.

The selector also rejected an implementation fix accompanied by its owner-test
source. It now resolves same-package ordinary tests directly dependent on declared owners,
accounts for their source files, and explicitly keeps those tests required.
Every direct consumer must still be accounted for; shared helpers, unrelated
production inputs and BUILD changes retain broad coverage. Test-only changes do
not activate replacement. The CI-driver regression cases and helper aspects pass
in [7a648970](https://app.buildbuddy.io/invocation/7a648970-0ad5-4a4f-9ef1-fc4e577fc728).

## Remaining exhaustion wait

The retained Gateway exhaustion target takes 271.6/270.0 seconds in those
arms. Its deliberate 120-second reconnect window is now a measured candidate
tail. A separate manual `test_gateway_budget_short_candidate` reuses the exact
test/scenario body with the previous explicit 45-second fixture. The required
target and frozen comparison keep the 120-second default. Rendering verifies
the two fixtures differ only in that setting, and production rendering still
omits the override. Those checks, deterministic rendering and the changed
helper-library aspects pass in
[644f1468](https://app.buildbuddy.io/invocation/644f1468-f1d9-4b62-9e31-c0c4022b05b2).
The deployed manual case subsequently passes in
[eaa3eca2](https://app.buildbuddy.io/invocation/eaa3eca2-6381-4aeb-b5c5-913ee4088d48)
at 228.8 seconds. This uses a working-tree overlay on `38f6c8e723`, not the frozen
graph source alone. The earlier command
[6e38fc22](https://app.buildbuddy.io/invocation/6e38fc22-06bf-4cb5-b6a1-241daf7e76dd)
exited 4 after CI's manual-tag filter excluded both targets; it executed zero
tests. The corrected command preserves the live-API exclusion and explicitly
opts into the two manual targets.

This revives the bounded experiment in [iteration 2](test_architecture_iteration2.md)
because the whole-graph screen now identifies that wait as a limiting retained
path. The five-second attempt failed real DNS/reconnect recovery; the previous
45-second attempt passed once. Forty-five seconds retains the exhaustion,
unknown-effect, no-replay, explicit recovery, once-only marker, History and next
input assertions, but does not provide this case's full-default-duration
exposure. Adoption needs an explicit coverage decision and new whole-graph,
reliability and cost evidence; its nominal 75-second timer reduction cannot be
added to other isolated savings.

The next retained target in the first candidate is Dispatcher at 246.5 seconds.
The manual `test_dispatcher_sharded_candidate` uses the existing Bazel/pytest
sharding support to execute its three collected cases on three fresh K3s stacks.
It reuses the unchanged queue, replica-failover and wake/notification bodies,
with no deselection. The required Dispatcher target remains unsharded. This
candidate duplicates two stack setups and raises simultaneous resource demand.
It passes in the same `eaa3eca2` invocation with a 233.3-second longest shard.
Artifact review confirms the exact three-case union, once each across three
distinct actions. Its wall time lies inside the two recorded ordinary outcomes
(218.0–246.8s), while test CPU rises to 799.3s from 301.4–388.6s and estimated
CU-min rises to 30.48 from 14.58–16.50. Each shard creates its own session-scoped
stack. Reject this arrangement for promotion: it duplicates setup without a
clear wall-time win. Retain the manual target only as a reproducible experiment;
do not repeat it unless the setup/lifetime arrangement changes.

JUnit elapsed values include fixture work and do not isolate scenario execution.
The shorter-budget case is one changed-contract exposure, not a matched latency
gain. Neither candidate is adopted, and no complete graph containing them has
been executed.

## Prepared Firecracker substrate

The manual `test_firecracker_prepared_baseline_candidate` runs the unchanged
two-case Firecracker source, including wrong-issuer and checkpoint/thaw behavior.
The [cold exposure](https://app.buildbuddy.io/invocation/8aacd3aa-a1a4-42f6-a76e-f3f183ee17e9)
and [warm exposure](https://app.buildbuddy.io/invocation/27d6c004-c525-42a1-9698-06dd5b762d88)
both pass at `38f6c8e723` plus working-tree overlays, with identical executed
TestRunner Action and Command digests. The second attaches to the same K3s
PID/start ticks/boot ID, namespace/node UIDs and KVM plugin UID. Per-run test
namespace, PostgreSQL Pod, root certificate/Secret, collector, Sandbox and
live-guest Pod identities differ. A successful trust-only guest Pod UID was
not recorded; fresh state is proved only for the recorded resources.

Native time falls 302.222s → 275.648s (8.79%); physical action execution falls
259.653s → 233.914s (9.91%). Setup falls 48.375s → 21.772s while the two case
spans change by +1.836s and −0.987s. Estimated CU-min falls 13.045 → 11.754;
actual CPU, memory, disk and queue data are unavailable. One cold/warm pair is
mechanism evidence, not whole-graph, p90 or flake qualification.

Do not adopt v1. The KVM executor bind-mounts a host-shared `/var/lib/k3s-env`
while the ownership marker lives inside the recyclable container. A paused
retained K3s process can outlive its action while a different runner uses that
shared data directory. One active action does not establish one live server.
The explicit v1 cleanup target requires the completed warm receipt, exact live
process and full API identity, matching binary/data-dir argv and an unchanged
clean marker before stopping that owner. A success artifact must prove the
recorded process stopped and marker was removed; a missing marker is an error.
The target builds, but cleanup has not run: automatic approval review rejected
the shared-store mutation, and explicit operator approval is pending.

The new manual v2 family places KVM-retained data inside its private persistence
root and records that path in the marker. Path-bound stop refuses unknown or
mismatching locations; non-KVM and ordinary fresh-stack defaults remain intact.
This removes the identified shared-data coupling but has no cross-action
qualification yet. Keep ordinary Firecracker non-recycled and fresh.
