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
