# Common backend changes and the next test partition

The journal-only shadow policy matched none of the 30 older reviewed changesets.
Its synthetic graph's 29.3% lower two-run midpoint does not establish useful
selection for routine backend PRs. Account for real source, test and BUILD
changes together before another speed screen.

## Applicability audit

The 100 most recently merged PRs to `devel`, ordered by exact `mergedAt`, cover
2026-11-13 12:18:27Z through 2026-11-14 08:55:11Z. Complete paginated file lists
match each PR's `changedFiles` count. A 314-PR date-window search and separate
500-PR metadata listing checked the selection boundary. This is a path/PR
population, not a latency or failure-rate sample. Raw metadata stays private.

46 PRs changed backend implementation/protocol inputs; 21 only tests/support;
16 only documentation; 10 only build metadata; seven only paths outside
Agentplane. Fixture modules and `conftest.py` count as test support. Comment-only
implementation/protocol edits remain source inputs.

Among the 46 backend-input PRs, 33 also changed Agentplane BUILD files, six proto
files, 12 crossed first-level product source roots, and 33 also changed docs.
These categories overlap. Runner Gateway appears in 17 PRs, App in 10, Central
Sampler in six and Notification in four; excluding the broad cleanup #16210
gives 16, nine, five and three. These overlapping domain counts do not prove
that one policy can cover each domain.

## First real class: App handlers and server composition

| Change                                                                          | Direct and composition tests                                                                              |
| ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| [#16320](https://github.com/agentydragon/ducktape/pull/16320), thread metadata  | `httpapi/threads:handler_test`; server `:threadreadapi_test`                                              |
| [#16326](https://github.com/agentydragon/ducktape/pull/16326), Session handlers | `httpapi/sessions:handler_test`; server `:sessionapi_test` and changed `:runnerapi_test`                  |
| [#16329](https://github.com/agentydragon/ducktape/pull/16329), config/status    | server `:appconfigapi_test`, `:statusapi_test`, `:schema_export_test`; build production `httpapi:httpapi` |

Labels above are relative to `//agentplane/app/internal/`. All three change local
BUILD files and related production owners. They are only 3/46 backend-input PRs;
this class alone cannot qualify the aggregate median objective. Their App paths
exist unchanged at frozen source `38f6c8e723995efb5c8372c5f81bbd30d9e0621f`,
integrated base `bb7c716f5a97fe4946acb10f0dafa3d69ff76d76`, and observed devel
`bed6eb470b807ba85dee2a4822500d7f0617d833`. No historical path remapping was used.

The current journal-only policy rejects all three. A named domain alone does
not account for BUILD changes. Inspect changed source/rule owners in both
graphs and retain every affected build. The installed bazel-diff's known
package-default fingerprint gap also applies to ordinary BUILD files; direct
hashes alone do not prove that every BUILD effect was accounted for.

Exclude streaming/proxy extraction #16309, History/SSE retirement #16244 and
cross-service TLS #16332 from this first class. A source's membership in the
monolithic server library does not prove replacement coverage for all its behavior.

## Integration witnesses and next experiment

`//agentplane/app:test_api` exercises real App views, config/status, auth and
OpenAPI routes, but does not prove real Session admission and History integration.
The bridge case `test_history_openings_give_a_session_the_app_did_not_start_a_thread`
creates a Session through Dispatcher, then requires the production History
watcher and App HTTP to expose the exact Thread and expected Sandbox summary.
It is the smallest existing cross-process Thread witness, currently bundled
with four-shard bridge recovery.

The ordinary `//agentplane/app:test_thread_routes` now exposes that unchanged
case and adds App HTTP Session creation, real Dispatcher acceptance and exact
Thread/History reads without model turns or restart faults. The existing
replica-exit bridge case retains those recovery guarantees.

The [positive run](https://app.buildbuddy.io/invocation/9d64351b-500f-4da8-8786-6f8260b5bbc4)
passed all four Claude/Codex cases: 53.576s pytest, 85.618s Bazel elapsed,
76.885s TestRunner execution and 142.520 CPU-s. Requested resources were three
CPUs, 7.5GB estimated memory and 10GiB estimated free disk; observed peak memory
was 1,038,712,832 bytes. Requested estimates are not observed capacity or billing;
disk use is unknown. This is one focused run, not a baseline comparison.

The [wrong-Thread route control](https://app.buildbuddy.io/invocation/d80323d4-0491-44a5-9cc5-5d7b8f6bb26e)
returned HTTP 200 with the wrong Thread ID and failed all four exact-identity
assertions. The harness-only fault was restored. Root source review verified
the moved case and all 19 remaining bridge helper/case ASTs are unchanged.
The only subsequent bridge change corrects its module description. These proofs
use source `38f6c8e` plus documented working-tree overlays, not exact-head full CI.

The partition reuses the existing fixtures and adds one focused test module
and target. Its two new harness cases add work before any broader test selection
is replaced. Future selection must own the corresponding route contracts;
passing this target does not justify dropping the bridge recovery target on
all App changes. Measure the complete graph and resource delta before adoption.

Pod preset/bootstrap/session launch, VM supervision, Firecracker checkpoint,
trust/mount behavior, projected credentials, MCP Policy, Gateway roll and History
outage remain with their actual owners. Incidental App setup does not transfer
those assertions to handlers. Every substitution needs an assertion-to-witness
map and a negative control for the relevant defect.

The [full-universe reverse-dependency query](https://app.buildbuddy.io/invocation/1b1b8f13-6cec-414a-b07c-61138976a8db)
returns 132 test targets for the server, threads, sessions and HTTP API owners.
The [exact CI tag filter](https://app.buildbuddy.io/invocation/f72c01a4-6a8a-405d-ae3d-558110ef1395)
leaves 110 ordinary targets. Firecracker, Pod, VM, MCP Services, App Bridge and
Thread browser acceptance all remain selected.
[Dependency paths](https://app.buildbuddy.io/invocation/fee32392-d46e-46a1-8c13-1724f86dbee9)
trace the platform suites through `app:image`, `app_binary_layer`, the App
executable, `cmd_lib`, `server` and `httpapi`. Bridge/browser similarly consume
the App binary. This query uses current owner graphs, not actual historical
base/head hashes; selected labels are not proof of fresh physical execution
under ordinary caching.

Splitting the small route target alone therefore leaves every competing platform
tail. A 30% claim needs an additional architecture change: separate incidental
App setup from the actual VM/Pod/Services assertions, or qualify explicit
contract-specific substitutions while keeping each important App and platform
witness. Do not report this partition as already removing those suites.

Then measure the changed graph and complete required gate with frozen
source/input identities and ordinary caching. Expand to common
Gateway/other owners where distinct contracts and measured costs justify it.
Include ineligible PRs in the representative backend result. The goal remains
30% lower median complete feedback including reruns, no worse p90/flake rate,
preserved important coverage, and documented compute/maintenance cost.

## Native platform driver milestone

The new `system_tests/driver/session.py` drives an exact environment-hosted
Session through real Dispatcher and History clients. Create and input submissions
are sent once; only History reads reconnect. An initial missing History attachment
may wait, but a Session disappearing after a healthy read fails. Placement stays
pinned to its original Environment UID. Setup output and completed turns use the
same correction-aware page state extracted from the App reader. App and native
drivers share the existing exact-admission/terminal interpreter, including cursor
advancement on failed turns and rereading entries beyond a returned terminal.

The ordinary
[`test_session_driver` proof](https://app.buildbuddy.io/invocation/fe144b9c-d652-486f-8d8c-a2211888713a)
passed one real-process case in one attempt. It creates a Session with setup,
checks stdout/stderr, leaves an earlier completed turn unread, observes an actual
History RPC timeout while that fixture-owned process is paused, and observes
Runtime finish through Gateway while History cannot answer. After History
resumes, the collector returns only the requested turn; a third input completes.
The case requires three exact admissions, ordered answers, completed statuses,
contiguous History cursors and exactly three model requests. Its
[dependency query](https://app.buildbuddy.io/invocation/ea165288-9a17-4d69-b866-ffaf364e6095)
contains no `//agentplane/app` targets.

This is an App-free Session driver proof, **not an App-free platform suite or a
latency comparison**. The source is frozen `38f6c8e` plus a recorded 57-file overlay
from local `0b6df927` and these working changes; it is not exact-head full CI.
The native case took 50.295s, pytest 51.072s, TestRunner 74.718s and Bazel 82.145s.
Observed CPU was 129.294s and peak memory approximately 1.087GB. Requested estimates
were 3000mCPU, 7.5GB memory and 10GiB free disk; estimates are not observed capacity
or billing. Disk consumption is unknown. This new ordinary test adds work until
platform dependencies or selected tests are actually removed.

Existing Agent and baseline regressions passed in the
[first batch](https://app.buildbuddy.io/invocation/3ac1ef4c-dc7a-4dd8-9f3b-14c95c92be1f).
Two new test harness errors were corrected: a nested autospec over already mocked
gRPC callables and a positional argument to a keyword-only fixture parameter.
The [focused rerun](https://app.buildbuddy.io/invocation/9c3ac457-8c78-4654-b565-b2e5c3a8a2ed)
passed 14 native-driver safety cases and nine stack lifecycle cases. The
[explicit changed-library build](https://app.buildbuddy.io/invocation/b15649dd-cb24-43c3-8395-6079f47a620a)
passed all 11 libraries and their lint/type aspects. These runs do not estimate
fleet flakiness or qualify a replacement gate.

### Private fresh-KVM prerequisite

Fresh non-retained KVM stacks need a private store and must skip the legacy
previous-server stop. The first candidate used `/var/lib/k3s-persistence-v7/data`
on the action overlay root. Its guard rejected existing ownership markers/data
(including symlinks) before stack setup; non-KVM and retained-v2 paths stayed
unchanged. The pending rejected v1 cleanup remains unexecuted.

The [read-only preflight](https://app.buildbuddy.io/invocation/48d541fa-e122-405b-aad7-c66f3bfc72ad)
ran only `TestActionK3sIsolationPreflight` in the manual KVM test target and passed.
Its private diagnostic artifact records an overlay root with no propagation,
critical K3s/kubelet/CNI/log paths on that root, cgroup `0::/`, no pre-existing
private baseline and absent fixed CRI/debug sockets. It starts no K3s, signals no
process, changes no mount and never opens the shared-store contents. It does not
prove hostile-job isolation or inspect live Docker HostConfig. Namespace links
are observations, not proof that every namespace differs from the host.

The [physical Firecracker probe](https://app.buildbuddy.io/invocation/8651baa4-f752-4632-85d0-504f4828765f)
rejected that first placement: both collected cases errored before their bodies.
K3s never wrote kubeconfig and repeatedly reported that its overlayfs snapshotter
could not mount under the Docker overlay data directory (`invalid argument`).
Bootstrap reached its existing 180s deadline; the process was then terminated by
fixture cleanup. The error does not independently identify the filesystem
constraint; the workspace-placement retry tests that hypothesis. Raising the
deadline would not repair the repeatedly failing mount.
Bazel elapsed was 195.743s and TestRunner 189.285s. Debian package 404 messages
were fallback warnings; they did not prevent test execution. No platform case
passed in this run, and no shared-store cleanup was executed.

A second [read-only workspace observation](https://app.buildbuddy.io/invocation/b51aed00-3480-4d78-a66a-8913dd0e8b0e)
confirmed that Bazel's absolute `TEST_TMPDIR` resolves onto the unique action's
ext4 workspace mount, together with `TEST_SRCDIR`. The corrected candidate uses
`TEST_TMPDIR/k3s`, requiring an existing absolute caller-owned test directory and
an absent data path. This avoids a shared K3s store, extra mounts, loop devices
and memory-backed layers. Physical qualification of that corrected placement is
recorded below; the read-only observation alone did not establish compatibility.

The [corrected offline checks](https://app.buildbuddy.io/invocation/18fa6181-44f9-4ecc-a72b-f9931811ffa9)
pass all 39 baseline and 11 stack cases without failures, errors or skips, together
with explicit lint/type checks for both changed libraries. The source manifest
records frozen `38f6c8e` plus 59 overlay paths; only the four fresh-store/lifecycle
files changed from the read-only workspace probe. Its SHA256 is
`8a399d249f6c6e4d352e993f56471190bff0a1b60595bf12852a00f95ce72b9d`.

The [corrected physical Firecracker run](https://app.buildbuddy.io/invocation/d0983102-efe1-4dbf-8b4b-f8314d9d09ca)
passes both ordinary cases, in one run and one attempt, with no failures, errors
or skips: live-guest preservation and refusal of a cluster-CA leaf naming the
Gateway. JUnit reports 246.889s; the cases report 204.183s and 42.487s (including
fixture exposure). TestRunner execution is 254.857s; Bazel elapsed is 290.188s
with a 259.86s critical path. These clocks overlap and are not additive.
K3s writes kubeconfig, and the captured data paths are all under the private
workspace's `k3s` child, with no legacy-store or overlay-root candidate paths.
The existing bootstrap span is 8.320s and stack setup 74.649s. The only changed
runtime choice from the failing placement is the data path and its guard; this
supports using the action's ext4 workspace for the fresh store. The run still
uses the existing App-dependent platform suite; the native lifecycle/profile
cut is separate work.

TestRunner CPU, peak memory and disk consumption are not exposed by this
execution record. Its queue timestamps are inverted, so queue duration is
unknown. This is one successful fresh-store run, not a matched latency comparison
or a p90/flake qualification. Shared-store cleanup remains unexecuted.

Private storage also gives up cross-action CRI image retention. Its effect on
cold image imports and full-suite p90 remains unmeasured; do not call it a latency
improvement. The manual preflight adds diagnostic maintenance but no ordinary CI
execution. The native driver reuses shared pagination and turn interpretation
rather than maintaining a second parser.

### Dependency cut and remaining qualification

- Qualify the implemented VM/Firecracker lifecycle operations and diagnostics through native
  EnvironmentService/SandboxControl/History clients, with an explicitly authorized
  fixture principal that does not require an App process or App-issued identity.
- Verify the separate rendered profiles and image maps without App. Audit
  inherited conftest dependencies as well as direct imports; removing Python calls
  alone leaves `app:image` in the graph.
- Retain real App route/composition/fold assertions in ordinary App integration
  tests. Keep issuer, credential rotation, controller, storage, guest supervision,
  checkpoint and recovery assertions with their actual platform owners.
- Re-query actual dependency paths and replay representative real source plus
  BUILD edits under ordinary caching. Count ineligible changes in the aggregate.
- Measure the complete required gate, reruns, competing tails, p90 and busy-load
  resource cost before adopting any selection substitution or claiming 30%.

### Native platform dependency migration

The same architecture branch now implements native Environment lifecycle, Session/History and
Runner-control clients for the ordinary VM and Firecracker targets. A dedicated fixture
ServiceAccount receives audience-specific tokens bound to one Ready fixture Pod UID. The native
profiles retain real PostgreSQL, platform binaries, controllers, PKI and guest images, and omit the
App Deployment, image, database role/bootstrap, OIDC and App-issued tokens. Their idle identity Pod
uses `/bin/sleep` from the already-required pinned PostgreSQL image; no extra harness image is added.

The required Firecracker target retains its two cases. The required VM target retains the one case
that concurrently exercises both Claude and Codex, including setup fencing, private systemd
supervision, issuer binding, Egress/Notification CLI routes and process traces. Native History
checks retain exact command admission, ordered terminal events and the exact Sandbox UID fold.
App Thread projection remains with the ordinary focused App routes and Bridge contracts. Manual OOM
code was separated from the required VM scenario; it remains a manual deployed test.

The first dependency query exposed an App HTTP client imported indirectly through shared credential
checks. Moving those checks into `driver/workload_credentials.py` removes that dependency without
changing their four function ASTs beyond the declared helper renames. Three Firecracker guest/cert
probe functions are also AST-identical. The repeated [query](https://app.buildbuddy.io/invocation/254f8996-bf00-46d8-ba09-71f7a1c405ea)
contains no App executable, image, internal server, HTTP testing client or App-backed conftest for
either ordinary target. Shared rendering still imports App configuration types; this is not an
empty-App-prefix claim.

At exact published source `04cbff477648804d7f905aef148065e1ad55f353`, the
[four-platform reverse-dependency query](https://app.buildbuddy.io/invocation/ce224205-7707-4240-8c4b-9a5520a3b468)
returns only Pod and MCP Services as consumers of `//agentplane/app:image`. VM and Firecracker
are absent. This establishes a real graph boundary for App executable inputs; shared App config
types still affect their renderers. It does not establish historical base/head hash selection or
fresh test avoidance under ordinary caching. The two remaining App-dependent platform tails must
be accounted for before converting this cut into a complete-feedback prediction.

The [14-target caller build](https://app.buildbuddy.io/invocation/f0f076a4-82fa-45b5-a7d5-673c5e2bcfe2)
passes with lint/type aspects, including the migrated ordinary and manual VM/Firecracker variants
and MCP callers. Earlier combined validation exposed required config values, missing direct
protobuf stub dependencies, mock typing and native plugin wiring. The first physical native run
stopped before K3s because the removed conftest had owned `k3s_binary`. That fixture now has a shared
App-free plugin, and native plugins load before collection. These are implementation failures,
not samples of production CI flakiness.

After rebasing onto observed devel `bed6eb470b807ba85dee2a4822500d7f0617d833`, the exact
published source `fc38753823252f883714445993f0ae8d9211aa2a` passes
[all nine unit/render targets and 36 explicitly named libraries](https://app.buildbuddy.io/invocation/949f8ca6-9550-4718-8e3b-537c473d0931),
including their lint/type aspects. That check has no source overlay. Earlier frozen validation
failures included plugin visibility and nested-conftest plugin registration, both corrected before
this pass. Remote metadata for the earlier overlay experiments identifies the initial checkout,
not the exact tested tree.

The [first real native platform deployment](https://app.buildbuddy.io/invocation/5ffe5074-1533-4b35-8374-593359d10499)
used frozen source manifest `5d97bed218a1127c6793e4d0385c2c47fcf701ed81e46ff0142130fc0a2fa182`.
Both VM and Firecracker stopped during stack setup: Actions could not read its settings file.
`ServiceWorkload.project_tokens()` ran before the settings/TLS mounts. With no earlier optional
Secret mounts, its JSON patches replaced the volume arrays and discarded those later mounts.
Moving token projection last repairs the generator; the independent regression exercises the
normal Actions construct without optional Secret mounts. On devel, the corrected regression
[fails on the old generator](https://app.buildbuddy.io/invocation/60032484-6fef-4f34-af7d-5181f087d685)
at the missing settings mount and the
[complete Actions target passes with the repair](https://app.buildbuddy.io/invocation/093dd080-7668-40f2-8131-64ab2ed7d687).

The first full CI check after that repair exposed a client audience error in the migrated MCP
Actions cases: EnvironmentService uses the Dispatcher's method audience, while SandboxControl
uses the Gateway audience. The native clients now route those separate Pod-bound credentials to
their actual methods; no production authentication rule is widened. Published source
`04cbff477648804d7f905aef148065e1ad55f353` completed the
[focused physical and owner checks](https://app.buildbuddy.io/invocation/c1ace771-db7b-4c7c-91e3-75080abdf2f0).
The check includes ordinary VM, Firecracker, MCP Actions, Actions rendering and native-client
targets. Five targets pass in one attempt: both original Firecracker cases, all five MCP Actions
cases, 14 Actions rendering cases, three Environment transport cases and four native-platform
cases. Firecracker TestRunner execution is 254.256s; its cases report 202.485s and 42.391s.
Those durations do not establish a speedup against the earlier App-dependent run, whose source,
imports and concurrent workload differ. The benefit currently proved is the dependency boundary.

The first VM case completes both harness scenarios, then fails the retained process-trace contract:
the native Session driver's `system_test.session.create` span omits `sandbox.name`. Its added
span now carries the exact Sandbox name/UID and harness from the validated placement. Inspection
of the same private artifacts also finds no Runtime CreateSession sharing the driver trace:
the native Dispatcher test client did not inject W3C trace context, unlike the App HTTP test client.
A real gRPC wire regression
[fails on the old transport](https://app.buildbuddy.io/invocation/ff4e8fbc-64fa-41d9-a1d0-9cf1bd539814)
because the server receives no `traceparent` for an active driver span. The Dispatcher test client
now injects the current context per unary call, retaining rotating bearer credentials and no
automatic mutation replay. The real trace assertions remain unchanged. The first full CI check also
exposes Dispatcher assertions whose expected caller lists predate the fixture principal. Both
lists now explicitly include that native caller beside App, Notification and Actions; native
test sends need the same opted-in wake behavior as the App sends they replace. The original
wake/no-wake cases and exact allowlist equality remain. These are implementation failures, not measurements of
devel's flake rate or evidence of a speedup.

Exact pushed source `6c31cfb9f7412506338a069fda13100fc291f993` now passes the
[combined physical qualification](https://app.buildbuddy.io/invocation/b31ff206-af86-4808-95ff-776a15f0905c):
five test targets, 11 cases and three explicitly named libraries/aspects, with no source overlay,
skips, failures or test retries. The VM process-trace contract passes. Each test has a fresh
physical execution in one attempt. Native Bazel elapsed is 347.022s; these are action clocks:

| Target                          | Cases | Execution |      CPU |         Peak memory |
| ------------------------------- | ----: | --------: | -------: | ------------------: |
| Native Dispatcher test client   |     4 |   15.051s |  12.773s |   548,794,368 bytes |
| Native Session driver           |     1 |   46.592s |  56.719s | 1,099,145,216 bytes |
| Firecracker                     |     2 |  241.044s |  unknown |             unknown |
| Dispatcher                      |     3 |  258.982s | 300.706s | 4,890,451,968 bytes |
| VM, concurrent Claude and Codex |     1 |  274.889s | 849.883s | 6,062,084,096 bytes |

Inner action queue is 0.058-0.059s except Firecracker's 7.952s. Disk usage is unknown.
This is one source exposure, not a matched timing comparison or p90/flake qualification.
The metadata-only repair invocation `2dba4751-321c-46ba-9aa1-49fd884d02d0` had a cancellation
request after diagnosis but nevertheless completed with the trace-parent and old caller-list
failures; it is neither a pass nor a successfully stopped experiment.

The [ordinary PR invocation](https://app.buildbuddy.io/invocation/b215237c-b0a9-5800-bbb5-6a15b278171f)
for the same PR head also passes all 2,005 test targets, of which 15 execute fresh. Native Bazel
elapsed is 627.656s. Pod is the longest fresh target at 532.0s, versus VM 286.5s and Services
285.3s. This changed competing tail justifies retesting the existing balanced Pod split; it
does not turn earlier unequal-cache measurements into a causal speedup.

### App-free central Services candidate

Published source `f919e2ed22f4245446f5bdd6e4b0b03480da8ffc` adds a manual
`test_services_central_native_candidate`. Its four tests call the same central-sampler scenarios
with unchanged timeouts, recovery faults and assertions. Dispatcher, History and Gateway readers
use audience-specific tokens bound to one Ready fixture Pod UID; the separate Gateway-to-sampler
credential path is retained. The suspend/resume scenario replaces only its incidental App
inventory read with the native Environment service. The rendered stack omits App, OIDC and MCP
Edge while retaining the real sampler, services, controllers, PKI, Pod and harness images.

The ordinary seven-case Services target remains required and unchanged in case ownership. Its
three App/MCP discovery, consented identity and policy-binding witnesses are retained. The
candidate is not promoted until physical acceptance, dependency inspection and comparison pass.
The [focused qualification](https://app.buildbuddy.io/invocation/fd790fe6-da40-460f-a3c6-df0324de6d4f)
passes on that exact source: native four cases, original seven cases, 21 render cases and four
named libraries/aspects. All three test targets execute fresh in one attempt without failures,
errors or skips. Native Bazel elapsed is 379.430s. The four-case native target takes 271.860s,
481.926 CPU seconds and 4,694,003,712 bytes peak memory. The seven-case original takes 308.526s,
599.724 CPU seconds and 5,840,916,480 bytes. These are unequal workloads, not a causal speedup.
The [candidate dependency query](https://app.buildbuddy.io/invocation/a5029663-f1fa-490d-8a69-0e7ebb7d9934)
retains only five App configuration modules; App executable/image, HTTP client and App-backed
conftests are absent. The full PR check catches an omitted expected output in render determinism:
the new variant is now included, retaining both independent-render and byte-equality assertions.

The three-case `test_services_mcp_candidate` now has its own entrypoint instead of deselecting
four tests from the combined module. Its DCR and policy wrappers retain their bodies; the full
consented-identity body moves to the focused `scenarios:external_connection` library, called by
both the original and candidate wrappers. The shared MCP scenario library gains no dependencies.
This removes central-only imports/dependencies from the candidate without duplicating assertions.
Its separate `mcp_services` fixture now retains App, Dex/OIDC, MCP Edge, Actions, History,
consented identities and real Pod/PKI integration, but omits the unused central Sampler image,
Deployment, database and model route. The ordinary `mcp` profile remains unchanged. Each render
selects one explicit profile; the independent determinism driver renders both. The original
seven-case target remains required. Exact-source dependency and physical union evidence for
the structurally separate three-plus-four graph is recorded below.

The manual `test_pod_trust_native_candidate` exposes four unchanged scenario bodies: certificate
reissue and the three wrong-issuer trust cases. It uses a Pod-only App-free fixture, retaining
real Kubernetes, PKI, issuer reconciliation, Runner and Gateway paths, with no unused central
sampler. Certificate reissue moves unchanged to an App-free scenario library with the original
30-second bound. The trust scenarios already used only one descriptor from the original pair;
the candidate creates one Ready Pod and verifies its exact Environment and Pod UID. Its logical
case names are preserved. Render invariants and independent-render byte equality include the
new profile. All original 21 Pod cases stay required. App Thread/feed/projection, model/preset and HTTP error assertions are
explicitly retained with their App-backed owners.

The manual `test_pod_without_trust_candidate` keeps those other 17 cases on the original
App-backed module with six slots/eight requested CU. It deselects exactly the four native
trust target cases. Physical qualification proves the 17-plus-four logical case union below;
no required selection changes.
The split adds a fixture lifecycle, so any latency benefit must include its CPU, requested
compute time and queue exposure. The six-slot trace ends with the Gateway refusing a wrong-issuer
runner address, while earlier App cases finish before it; that observed tail motivates this cut.

### Exact-source slice qualification

At `9b330689a24042259393a713b5f271b42bd7e669`, the
[Pod-native dependency query](https://app.buildbuddy.io/invocation/c4577721-8573-4e72-b99b-be0cd4ae8176)
and [central-native query](https://app.buildbuddy.io/invocation/76b9f4af-d1b1-4548-8ed8-617a0cc3e22c)
exclude App serving images/binaries, HTTP client and App-backed conftests. Five shared App
configuration modules remain. The
[MCP3 query](https://app.buildbuddy.io/invocation/8af9a101-68ae-41e6-8f08-8344a8eedbed)
excludes central Sampler image/binary and central-only scenario inputs.

The [first physical qualification](https://app.buildbuddy.io/invocation/12cdf1bb-c0de-400c-821e-8f79dcb4b275)
loses its outer runner with provider status 13, `stream closed with task still claimed`.
Its inner Bazel invocation is disconnected. Retained, exact TestRunner `ActionResult`s and
their matching JUnit output digests nevertheless prove all seven component actions passed:
59 cases, zero failures, errors or skips. The original Services seven equal the three MCP plus
four native cases. The Pod 17 plus native four equal all 21 original logical leaves; only the
original parameterized wrapper is normalized. Render contributes 23 cases and determinism one.
This proves component coverage; it does not convert the disconnected aggregate into a success.
Executor-internal attempts and old worker liveness are not inferred from stale execution indexes.

The [healthy follow-up](https://app.buildbuddy.io/invocation/b741d170-3bb3-4980-946b-c1f5f1c55030)
then passes the two remaining slices together: 20 cases and all eight explicitly named libraries
with the configured lint/type aspects. Each test has one recorded test attempt; both fixture
states are warm and cleanup completes. Pod17 physical execution is 302.694s, CPU 1,145.606s,
peak memory 5.586 GiB; MCP3 is 178.047s, CPU 352.908s, peak 5.099 GiB. Setup/case envelope/cleanup
are 35.630/167.470/63.647s and 59.114/37.737/59.364s respectively; these phases are not the entire
action. Pod node capacity is 11 allocatable CPUs and 20,012,876 KiB memory, distinct from the
eight-CU request. One turn-wait error span occurs inside a passing resume case; cleanup has no
error spans. This is correctness and phase evidence, not a matched saving or flake-rate result.

All three hosted required checks associated with head `9b330689` pass after one failed-job
rerun. Both CI attempts test synthetic merge `2aec7a80cb38d483634e9236616168ba5e9a9a06`, whose
parents are devel `ff801c85c732b368a67f0d6ada3bb487146abca8` and that PR head. Direct qualifications
above test `9b330689` without overlays. Preserve the provider-failed attempt in feedback/reliability
accounting; [the lease-stream note](../../../devinfra/buildbuddy/debug/2026_11_14_claimed_lease_stream.md)
records the mechanism and visibility limits.

The same-source Pod-only split screen completes in B3→A3 order:
[candidate](https://app.buildbuddy.io/invocation/a3515982-6fec-47d0-9ea8-84c661d6d45a)
and [control](https://app.buildbuddy.io/invocation/569d3f7b-a8f7-495b-b70b-2a63f82129ab)
both pass all 40 logical cases, with matching JUnit digests and one recorded test attempt per
target. Native Bazel elapsed is 516.925s to 384.420s (25.63% lower once), below the 30% screen
threshold. Known managed CPU ratio 1.1415, managed requested-CU execution ratio 1.1776 and actual
peak requested-CU ratio 1.2857 pass the predeclared 1.15/1.20/1.30 ceilings narrowly. Requests
peak at 28 versus 36 CU; KVM CPU remains unknown and separate. Pod reports seven versus eleven
allocatable CPUs and unequal warmth (control cold, candidate Pod fixtures warm). Firecracker
queue is 112.417s versus zero, with valid timestamps; it is not the last target. These conditions
prevent a causal or representative speed claim. This new architecture budget does not alter the
earlier six-slot single-fixture screen's failed 10% CU-time cap.

The candidate finishes on original Services (348.050s), followed by Gateway budget (313.130s)
and Pod17 (302.671s). The control finishes on Pod (483.748s), whose last case is the Gateway
refusing a wrong-issuer runner address. Services setup/case envelope/cleanup is
70.370/177.323/62.978s in B3 versus 64.651/176.647/61.986s in A3. The exhaustion target's case
envelope is about 165s in both arms. These competing tails motivate evaluating a composition,
rather than extrapolating an isolated target saving.

The next frozen-source screen includes all ordinary targets under `suites/...`, as confirmed by
the [Bazel roster query](https://app.buildbuddy.io/invocation/ee5d45db-7919-4cd7-981a-0bb653501781):
13 integration suites plus the live-harness unit target. The 16-target candidate replaces Pod21
with 17+4 and Services7 with MCP3+native4. It also uses the existing 45-second exhaustion profile
with the unchanged recovery/no-replay/next-input assertions. This explicitly removes 75 seconds
of default-duration exposure from that one integration case; production settings, other
recovery profiles and default/configurability unit checks retain 120 seconds. A matching case
name does not establish identical duration coverage. Keep the same architecture resource caps,
count every physical attempt, and leave the required roster unchanged while evaluating it.
The [control A4](https://app.buildbuddy.io/invocation/18ba7c7f-5eac-4920-8609-3ea521c86248)
and [candidate B4](https://app.buildbuddy.io/invocation/0097ec40-3d03-4bc4-b787-6de29bc27942)
both finish successfully at exact `9b330689`, without overlays. Each has 49 passing JUnit cases;
the logical case multisets match after normalizing only the intended scenario wrappers. Every
JUnit digest matches its successful physical ActionResult. The explicit budget-duration
tradeoff above still applies.

| Measurement, including every physical TestRunner execution | Control A4 | Candidate B4 | Ratio / screen                     |
| ---------------------------------------------------------- | ---------: | -----------: | ---------------------------------- |
| Native Bazel elapsed                                       |   516.795s |     574.657s | 1.1120; fails 0.70                 |
| Physical executions / final passing targets                |    14 / 14 |      17 / 16 | One additional failed VM execution |
| Known managed TestRunner CPU                               | 6,234.578s |   7,188.740s | 1.1530; fails 1.15                 |
| Managed requested-CU times execution                       | 13,470.184 |   16,635.356 | 1.2350; fails 1.20                 |
| Peak concurrent requested managed CU                       |         52 |           64 | 1.2308; passes 1.30                |

The candidate is 11.2% slower in this exposure. Its first VM operation has REAPI status 14 / exit
-2 after a provider Firecracker guest kernel panic; a distinct operation with the same action
digest subsequently succeeds. The failed operation consumes 166.683s execution, 167.027s worker
reservation and 643.406 CPU seconds. The successful one takes 354.855s and 1,149.802 CPU seconds.
Both remain in the resource and feedback accounting, although JUnit records only one passing
attempt. Their relationship does not establish which retry mechanism submitted the second one.
The [owning kernel investigation](../../../devinfra/buildbuddy/debug/2026_11_02_scheduler_kernel_panic.md#14-november-full-system-screen-recurrence)
records the exact signature and its attribution limits.

Pod is the control's last target; VM is the candidate's last target. Candidate Pod17, native
central Services and short Gateway budget finish in 305.7s, 277.2s and 216.2s respectively.
Firecracker CPU is unknown in both arms and is excluded from the known managed totals, not
treated as zero. Requested CU is distinct from observed node capacity; Pod nodes expose seven
versus eleven CPUs. Mixed fixture warmth, uncontrolled account load and one sequential pair
prevent a causal median claim. Keep all candidate targets manual. Neither this failed screen
nor component correctness qualifies complete required feedback, ordinary-cache applicability,
p90 or flakes.

The next decision is about environment lifetime and reliability, rather than further target
fanout alone. Retained fixtures spend roughly 38–45s waiting for namespace deletion within their
53–64s cleanup; the App-free trust fixture has the same pattern. A bounded metadata observer
preserves deletion/grace semantics while identifying the remaining Pod, PVC or namespace
condition. The existing KVM route also needs fresh-source feasibility and capacity evidence;
moving VM beside Firecracker onto one serialized worker is not a qualified improvement.

### Native fixture client shutdown

The [retained trust control](https://app.buildbuddy.io/invocation/80e62ff1-a172-441b-89ee-8c19ca8eb2a4)
identifies the fixture's native-client Pod as the last remaining Pod, without a finalizer or PVC.
Containerd sends SIGTERM with the normal 30-second stop timeout, waits 30.045s, then kills the
container; its exit status is 137. The rendered command is `/bin/sleep infinity` as PID 1, which
has no SIGTERM handler. This identity Pod is added to every fixture profile on this architecture
branch, so it also delays App-backed fixtures; it is not an App shutdown failure.

The repair uses a shell PID 1 with an explicit TERM handler while waiting for a background sleep,
the same signal-handling pattern used by production command Sandboxes. It keeps the same pinned
PostgreSQL image, Pod-bound identity, Pod grace, namespace deletion command and cleanup absence
checks. The [fixed-command run](https://app.buildbuddy.io/invocation/7cd3b0f9-c27c-4bbd-88cd-bca7292c06df)
passes the four trust cases, ordinary App-backed History-outage case and renderer/stack tests.
The native client exits about 69ms after SIGTERM without KillContainer or a nonzero exit event;
the runtime still requests the original 30-second timeout.

| Retained native trust fixture |  Before |   After |
| ----------------------------- | ------: | ------: |
| Namespace deletion            | 43.290s | 11.463s |
| Baseline cleanup              | 61.648s | 22.638s |
| Total stack cleanup           | 65.057s | 26.884s |

This removes the demonstrated grace-expiration failure and observes 38.173s less cleanup in
one pair. The fixed run has additional test targets and uncontrolled executor load, so it does
not establish a matched whole-graph speedup, median or flake rate. The native identity Pod was
introduced on this branch; removing its shutdown regression is not a measured saving versus
devel. The diagnostic observer adds bounded API reads and private output, not a new service,
image or persistent resource. Reassess that diagnostic overhead during final qualification.

### Ordinary native recovery fixtures

Three ordinary required targets now select the existing App-free central fixture:
`mcp:test_gateway_crash`, `mcp:test_gateway_budget` and `mcp:test_sampler_roll`.
Their test modules, shared scenario bodies and real platform
assertions are unchanged. They already admit Sessions through Dispatcher, inspect retained
History and drive real Pod executors through the native fixture principal; App HTTP and MCP edge
are not assertion owners in these three scenarios. The ordinary Gateway budget remains 120s.
The separate 45-second experiment remains manual.

The [dependency query](https://app.buildbuddy.io/invocation/5f2b7062-74dc-4351-b7c8-90a5bb65a88c)
contains no App executable/image, App HTTP testing client, ancestor App-backed conftest or
MCP edge service image for those targets. It retains five App and two MCP settings modules
used by shared rendering. This is an ordinary dependency cut, not a path-filter exception or
replacement-policy expansion. In the [physical run](https://app.buildbuddy.io/invocation/07a05dc6-c5df-45c6-b745-adde0381c625),
the three unchanged scenarios pass in 182.7s, 277.6s and 184.1s respectively. These are component
results, not a successful aggregate invocation or elapsed-saving comparison.

The fourth attempted migration, `mcp:test_history_outage`, fails its explicit assertion that
the App Pod exists and remains unchanged while History is unavailable. Keep that suite's
original App-backed fixture and all its assertions; its original fixture passes in the
fixed-command run above. This failed attempt changes the ownership decision rather than
justifying deletion of that guard. App routes, projection, OAuth, delegation and MCP assertions
also stay with their existing required witnesses. Exact-head hosted CI and ordinary-cache
selection still need qualification; no complete-feedback saving is credited yet.

### Pod partition and capacity screen

At frozen source `6c31cfb9f7`, the existing balanced Pod split is screened with all six competing
VM/Firecracker/Dispatcher/MCP Actions/Services/Gateway-budget targets unchanged. Candidate
[B2](https://app.buildbuddy.io/invocation/58a5cb3f-b6c7-41e2-9858-be783ba63f4d) passes eight targets
in 429.315s native Bazel time; control
[A2](https://app.buildbuddy.io/invocation/358bfd56-dcde-4480-8ebd-d34c6194a883) passes seven in
495.738s, 13.4% lower elapsed in this one pair. The 40 JUnit identities match as multisets;
all 21 Pod cases remain, with no failures, errors or skips. Pod actions take 257.920s and
384.804s versus the ordinary 452.003s. Known test CPU, excluding unknown Firecracker CPU in
both arms, grows from 3,660.154s to 4,165.295s (13.8%). Pod CPU alone grows from 1,037.989s
to 1,410.055s (35.8%). Peak memory is 5,535,584,256 bytes for the ordinary Pod action and
5,088,698,368 / 5,183,868,928 bytes for the separate candidate actions; their separate peaks
cannot be summed into an observed simultaneous peak. Candidate has two fresh non-test build
actions; control has none. Cache warmth and concurrent load differ. This is a selected-graph
screen, not complete required-PR feedback, causal median saving, p90 or flake qualification.

The case traces show stale balancing weights: 232.907s and 363.362s of actual case work. JUnit
durations also include fixture phases and must not be used as pure case weights. Setup and cleanup
are duplicated. The next manual candidate keeps all 21 cases on one cluster, with storage still
exclusive first, but requests eight compute units and runs four case slots instead of two.
It records actual Kubernetes node capacity rather than treating the request as observed capacity.
This tests resource organization without another fixture startup. Adoption requires the same
case multiset, no new failure/cleanup signatures, a materially shorter complete competing graph,
and explicit CPU, memory and requested-compute cost. One larger action may cost more or queue
longer; no saving is yet measured and the ordinary two-slot target remains required.

The first [four-slot capacity exposure](https://app.buildbuddy.io/invocation/7b26a47a-b973-4eed-b5c0-f318e51e37f7)
now passes at exact `a915738a26075f2b0126f5df65599008fe7d0524`: all 21 Pod case names match
the earlier control, and independent render determinism passes. Both targets execute fresh in
one attempt. Native Bazel elapsed is 539.360s for these two targets, a different graph from the
seven-target control. Pod physical execution is 442.477s, CPU 1,674.522s and peak memory
5,721,894,912 bytes. The earlier control is 452.003s, CPU 1,037.989s and peak memory
5,535,584,256 bytes. Against that reference, CPU is 1.613 times and requested compute-unit
execution time 1.958 times; both exceed the predeclared 1.5 screening caps. Peak memory is
1.034 times, below its 2.0 cap. Do not adopt this arrangement or claim a large elapsed saving.

Kubernetes actually reports 11 allocatable CPUs and 20,012,876 KiB of memory. The eight requested
compute units are a separate scheduler property. Existing spans show a 220.225s case envelope
versus 321.105s in the earlier two-slot control, but setup is 130.737s versus 33.305s and cleanup
62.533s versus 64.601s. The candidate imports cold images, including 59.544s for environment-tools;
the control skips already-usable imports. Sources and warmth differ, so this is mechanism evidence,
not a causal timing result. The same important case assertions and storage exclusivity pass.

Source `8c097bae2b14fb158910958917f236c69d54c9c7` consolidates the four/six-slot candidates onto
the ordinary Pod module; both original fixture/test function ASTs are preserved. The first
same-source capacity pair runs [six slots](https://app.buildbuddy.io/invocation/f87c7963-0e50-4d47-9fac-1a6e2dd0311d)
before [ordinary two slots](https://app.buildbuddy.io/invocation/9cae6b36-68c0-47fd-9e79-2847e5ff2be6).
Both execute the same 21 cases fresh, once, without failures, errors or skips; retained baseline
evidence reports warm infrastructure and complete cleanup in both arms.

| Measurement                          | Ordinary two slots / four requested CU | Six slots / eight requested CU |
| ------------------------------------ | -------------------------------------: | -----------------------------: |
| Pod physical execution               |                               513.545s |                       313.609s |
| TestRunner CPU                       |                             1,093.896s |                     1,012.582s |
| Peak action memory                   |                    5,540,950,016 bytes |            5,968,359,424 bytes |
| Stack setup / cleanup                |                      34.407s / 64.197s |              27.137s / 62.427s |
| Actual node allocatable CPU / memory |                      7 / 9,957,952 KiB |            11 / 20,012,876 KiB |

Physical execution is 38.9% lower and CPU 7.4% lower in this single configuration comparison.
Requested compute-unit times execution is 22.1% higher; peak action memory is 7.7% higher. CPU,
compute-time and memory ratios pass the predeclared 1.5 / 1.5 / 2.0 screening caps. Increased slots
and node capacity are the intended combined treatment, so this does not isolate either effect.
Specific workers, arm order and ambient load remain uncontrolled. Preserve the earlier cold
four-slot exposure and its failed screen; this result does not supersede that observation.

The next screen compares all seven competing targets at the same frozen source, control first,
then six-slot candidate. [Control A2](https://app.buildbuddy.io/invocation/88db0989-55ba-40f8-8a9a-94e87fb95a54)
and [candidate B2](https://app.buildbuddy.io/invocation/1d268405-f1e7-4a68-9649-12a174d5b62f)
both pass the same 40 logical cases, with seven fresh TestRunner actions each and no failures,
errors, skips or retries. Native Bazel elapsed falls from 489.497s to 373.066s (23.8%), below
the 30% goal. Pod remains last at 443.020s versus 330.949s physical execution; candidate
Services is next at 291.709s. Both Pod infrastructures are warm and complete cleanup.

Known TestRunner CPU falls from 3,736.954s to 3,697.784s (ratio 0.9895), within its 1.10 cap;
Firecracker CPU remains unknown in both arms. Managed requested-CU execution grows from
8,065.575 to 8,900.240 CU-seconds (ratio 1.1035), **failing the predeclared 1.10 cap**.
Peak concurrent requested managed CU grows from 28 to 32 (ratio 1.1429), within its 1.20 cap.
KVM explicit CPU/memory remain separate; per-action memory peaks are not simultaneous memory.
The eight-CU/six-slot arrangement remains manual: one ordered capacity comparison with
different node shapes neither meets all screening caps nor proves a complete-feedback win.
The ordinary two-slot target remains required until complete-graph and representative ordinary-cache
required-check, rerun, p90 and flake qualification support adoption. Exact `8c097bae` passes all
three required checks; that correctness result does not establish the performance goal.

The independent Actions repair is [PR #16386](https://github.com/agentydragon/ducktape/pull/16386),
merged on 2026-11-14 at 17:22:30 UTC from `d34c757904475f262cce517503a21d863e466684`.
All three required hosted checks pass, as do 25 generated-manifest cases with no staging snapshot drift. Its
[ordinary CI invocation](https://app.buildbuddy.io/invocation/786681d0-1e2e-5bdc-bff1-d58089c715ef)
passes. That narrow port does not depend on adopting this architecture.

### Stable Gateway rebase qualification

The rebase onto `0dc97aeb45` incorporates devel's stable Gateway Service and removal of
per-Sandbox control Services/EndpointSlices and Runtime Lease records. Branch-only native
readiness and direct-guest assertions still expect the removed objects. Ordinary CI
[c1939149](https://app.buildbuddy.io/invocation/c1939149-a21f-5d05-a14d-18dfca57faa9)
exposes the stale EndpointSlice wait in VM/Firecracker and the Lease 404 after successful
authenticated guest status. Raising readiness budgets would not repair either failure.

Source `241e6134` routes native Runner reads through the already forwarded stable Gateway
Service while retaining exact Sandbox identity. Direct guest readiness now proves a wrong
incarnation is refused with `ABORTED`, then checks correctly fenced authenticated status,
boot identity, running lifecycle/progress and empty Sessions. The removed Lease is no longer
a production boundary. Ordinary budgets, real guests, PKI and scenario assertions remain.

[Focused platform validation](https://app.buildbuddy.io/invocation/b2aa638a-1a48-4e60-962d-57ea312f4d1c)
runs `b0ca37f6` plus the three-file stable-route overlay. Both Firecracker cases, the managed
VM aggregate and four native Pod trust cases pass. The aggregate invocation fails because
the newly changed unit helper omits a required request timeout; that helper is corrected
in `241e6134`.

[Final guest validation](https://app.buildbuddy.io/invocation/25ad6f90-e553-43a9-a887-7b38f419e33f)
runs `b0ca37f6` plus the final nine-file overlay subsequently committed as `241e6134`. All four
test targets pass: native-client units 13.3s, Claude guest 117.7s, Codex guest 190.6s and
guest transport 64.9s. The aggregate fails a `no-any-return` mypy error in the helper's
protobuf response. With an explicit result annotation, the named library and its mypy
aspect pass [01a64ba3](https://app.buildbuddy.io/invocation/01a64ba3-d192-47e3-9b6e-62cfe0fdac7f),
using source `241e6134` plus that one-line overlay. These focused results establish current
route/fencing behavior, not a speed improvement against devel
or a green complete required gate.

The [full-system KVM candidate](https://app.buildbuddy.io/invocation/292cabf2-7824-469a-9a23-7b92f13ecad8)
was started at `b0ca37f6`, with the ordinary 120-second budget and 49-case composition. It is
intentionally canceled after identifying the same retired-object readiness regression;
the planned control arm never starts. The child reports disconnected and the CLI exits one.
Retain the attempt and its physical costs, but exclude it from paired timing claims. Do not
resume or replay it as if only log observation failed. A new comparison needs a newly frozen,
corrected source. The required selection policy and existing broad fallback remain unchanged.

The independent structured resume-refusal repair is
[PR #16393](https://github.com/agentydragon/ducktape/pull/16393), now merged in devel. Its first
CI exposure fails during PKI fixture setup; one unchanged-head rerun passes all substantive
required checks. The failure remains part of the reliability evidence rather than disappearing
from the denominator. No fleet rerun reduction has been measured.

### Expanded native Egress partition

The next candidate extracts three Environment-only Egress scenarios: command-box SVID and
revocation, standing-workload SVID and pruning, and two Sandboxes sharing a Kubernetes account
before one loses access. The real Action Service, Policy Controller, workload credentials,
proxy decisions, impersonation and denial assertions remain. Decision lookup consumes the
already observed Environment identity rather than depending on an App API read.

Shared Action CLI arguments and PolicyObjects move out of the App-backed MCP scenario module;
every caller imports their owning modules directly. PolicyObjects is an async resource context
manager, retaining cancellation-safe cleanup. Static comparison at the extraction source
preserves all 26 moved assertions and the original scenario list. This is not runtime proof.

The native target grows from four to seven cases. Devel `22d099a9` subsequently adds two
ordinary Pod authorization/lifecycle cases, so the integrated union is **23 cases: native seven
plus App-backed sixteen**. Both new devel cases remain in the complement and required suite.
The old 21-case and 49-case measurements above retain their original source-specific counts.
Run the updated partition and explicitly named helper libraries before a new frozen full-graph
screen. Keep both candidates manual; no elapsed saving or selector promotion follows from
moving scenarios or counting assertions.
