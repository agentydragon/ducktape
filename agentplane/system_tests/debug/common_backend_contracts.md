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

The VM case completes both harness scenarios, then fails the retained process-trace contract:
the native Session driver's `system_test.session.create` span omits `sandbox.name`. Its added
span now carries the exact Sandbox name/UID and harness from the validated placement. The real
trace assertions remain unchanged and require another physical pass. The full CI check also
exposes an old Dispatcher assertion equating all admitted callers with wake callers; it now
accounts for the explicitly authorized native fixture principal while keeping the wake allowlist
exactly App, Notification and Actions. These are implementation failures, not measurements of
devel's flake rate or evidence of a speedup.

The independent Actions repair is [PR #16386](https://github.com/agentydragon/ducktape/pull/16386),
open and unmerged at `d34c757904475f262cce517503a21d863e466684`. All three required hosted checks
pass, as do 25 generated-manifest cases with no staging snapshot drift. Its
[ordinary CI invocation](https://app.buildbuddy.io/invocation/786681d0-1e2e-5bdc-bff1-d58089c715ef)
passes. That narrow port does not depend on adopting this architecture.
