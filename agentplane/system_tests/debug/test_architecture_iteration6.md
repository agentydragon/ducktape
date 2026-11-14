# Guest platform routes and a smaller deployed VM witness

Source remains on [draft PR #16102](https://github.com/agentydragon/ducktape/pull/16102).
No required gate or scenario frequency changed. The complete-graph qualification
in `agentplane/plans/fast_ci.md` remains incomplete.

## Current required checkpoint

Head `542e6152491f16e1cd677ad25b81cd5b7243cef8` passed all three required checks.
Its Bazel invocation is
[`657b546d`](https://app.buildbuddy.io/invocation/657b546d-1a27-599f-8f0a-ff2bd114ad28),
with tested merge `569b82520907d2c89e6c0f501e67b7ef2ce4f7e0` and parents
`a1121a0ad80a6c65485f177e3d55a682cddd6249` and the source head. Native elapsed
time is 549.365 seconds. The source branch's prior merge from devel is not the
same thing as the base of this tested merge.

[`test_architecture_required_542e615.json`](test_architecture_required_542e615.json)
retains all 13 K3s-suite target summaries: the filtered API page returns 12,
and an exact-label query supplies the missing VM target. These are summary
clocks, without a fresh-execution, JUnit, cache or CPU audit.

| K3s target   | Summary duration | Summary completion, UTC |
| ------------ | ---------------: | ----------------------- |
| Pod          |         404.835s | 21:19:04.948            |
| VM           |         324.917s | 21:18:13.683            |
| MCP services |         330.881s | 21:17:31.329            |

Pod ends 51.265 seconds after VM within this cohort. This supports working on
both tails; it does not bound complete PR feedback, because other tests and
workflow gates are outside this summary sample. The earlier 2196229 checkpoint
has a different tested source and selected population, so their elapsed times
are not a matched comparison.

## Packaged guest platform routes

Both manual Session contracts keep their original two real harness Shell turns.
The second turn also runs the packaged `agentplane` CLI through the real sidecar
and Egress proxy to production Egress and Notification processes. Each service
uses the caller's existing API server, Sandbox, Identity and CA, plus a separate
database in the existing PostgreSQL fixture.

The Notification driver calls production `serve`; its explicit test-only
kubeconfig boundary introduces no production setting. The production executable
owns migrations. The driver seeds a real `store.Subscribe` row for the caller's
Identity and Sandbox UIDs and postpones its source worker because this contract
does not run Action Service. The guest command contains no expected row ID.
The assertions compare the returned subscription ID, creation key and Action
request ID against the independently captured seed metadata. Egress must return
the exact Identity. Forged caller bearer credentials are deliberately supplied
to the local CLI route, whose sidecar owns outbound authentication.

After the guest stops, persistent host mTLS channels first confirm the same
positive reads. Updating the same WorkloadCredential's credential ID and URI must
make both services reject the old SVID with `UNAUTHENTICATED`, while the
WorkloadCredential UID and Pod UID stay fixed. This is a per-RPC revocation
contract, not a post-rotation guest CLI or controller-issued rotation witness.

The original shared setup probe and independent cleanup-fence Session remain
after the two Shell turns. The 120-second QEMU process bound is unchanged.

## Implementation failures retained

- [`b1e55f70`](https://app.buildbuddy.io/invocation/b1e55f70-8748-4bff-9619-ec6fcc42d516)
  stopped on mypy before either guest test executed. The fixture dataclass used
  the synchronous Notification stub type; it now uses the generated async type.
- [`b1582db4`](https://app.buildbuddy.io/invocation/b1582db4-ba88-48ec-8bac-2e316779deb6)
  stopped on Ruff's compound-assertion rule, also before test execution.
- [`e7a6dbad`](https://app.buildbuddy.io/invocation/e7a6dbad-406f-462b-be87-768f5f756dfb)
  ran both guests. Both actual CLI routes returned the seeded row, but the
  assertion wrongly expected an array: CLI list output is one JSON object per
  line. Cleanup additionally reported Notification exit by SIGINT.
- [`4787345e`](https://app.buildbuddy.io/invocation/4787345e-c7ae-4d07-b7ec-e4c70dadf48b)
  ran both guests. Codex reached cleanup after its assertions; Claude's strict
  trailing-output assertion encountered the native harness's system reminder.
  Notification cleanup exceeded its unchanged 10-second deadline in both tests.
- [`ec2558a9`](https://app.buildbuddy.io/invocation/ec2558a9-92dd-4591-8c0d-f0247397b4e5)
  ran both guests with the explicit CLI completion delimiter and drained child
  stdout. Both bodies reached final teardown after the CLI, setup, History and
  per-RPC revocation assertions; both still failed Notification shutdown. Draining
  stdout did not resolve the wedge. These three runs contain six unique NEW
  TestRunner records, without additional or merged executions.

An explicit shell completion delimiter now separates CLI JSON from the Claude
wrapper text. Notification cleanup uses the ordinary TERM signal and drains the
child stdout pipe while waiting. A cleanup failure remains fatal on a healthy
body and is attached to an existing primary exception rather than masking it.
These corrections need a complete passing run; the failures above are not
healthy reliability exposures or a demonstrated flake-rate repair. Timeout now
requests Go's standard SIGQUIT goroutine dump before a bounded kill; the service
grace remains unchanged. A TERM-only Go reproduction passed in
[`1e313f9e`](https://app.buildbuddy.io/invocation/1e313f9e-37c7-4743-a74c-5a935d7eb854).
A Go reproduction with driver-owned seed, same-record credential rotation,
old-SVID refusal, new-SVID owned-row read and TERM shutdown also passed in
[`0bbd486a`](https://app.buildbuddy.io/invocation/0bbd486a-d7be-44b7-bf09-9b771c257917).
The Python-only fixture reproduction
[`97f376d7`](https://app.buildbuddy.io/invocation/97f376d7-4901-4599-915c-5c03e941f674)
then failed the same TERM deadline after its owned-row and rotation assertions.
QEMU, Egress and the sidecar are unnecessary to reproduce it. Four preceding
analysis/type-check attempts ran no lifecycle action.

The pinned rules_go `bzltestutil.Wrap` source explains the parent difference:
`XML_OUTPUT_FILE` enables a wrapper that re-execs the binary and ignores TERM.
The Go parent is already inside that wrapper and inherits `GO_TEST_WRAP=0`;
Python is not. The Python fixture now explicitly sets `GO_TEST_WRAP=0` on its
test-driver child, so the owned PID handles TERM and the outer Python test owns
JUnit. No production shutdown code or grace period changes.

With that single lifecycle change, the same Python-only test passes in
[`c142d99a`](https://app.buildbuddy.io/invocation/c142d99a-b2e0-447e-adcb-8b3908f3a2dc):
one fresh test, 45.5 seconds, with the `:process` type/lint aspects named too.
Both full guest contracts pass in
[`d68330b6`](https://app.buildbuddy.io/invocation/d68330b6-2d7c-4dbe-b3e7-ade6cd9fc5f7).
There are two unique NEW completed TestRunner actions, two nonempty JUnit cases,
and zero failures, errors, skips or additional/merged test executions. Codex
process time is 140.304 seconds with 310.323 CPU seconds; Claude is 198.395 seconds
with 411.909 CPU seconds. Invocation elapsed is 239.693 seconds. These are separate
clocks, not a matched CI speed comparison or production reliability-rate sample.

The wrong Notification server-URI control
[`d2cba508`](https://app.buildbuddy.io/invocation/d2cba508-bfdb-4cf5-b23f-ce9eeb35b115)
changes only that route's expected peer URI to the Egress service URI. The normal
seeded-row preflight succeeds; the guest CLI instead returns an error object,
which fails interpretation as the required owned subscription. Cleanup completes.
This rejects the changed route; the precise TLS error payload has not been
established from the captured failure, so do not claim a specific observed TLS
error code or message. This deliberate failure is outside healthy exposure.

## Deployed witness

Commit `194810646c5122f5574ae7d6ad9fe02044cc7113` adds the two-guest candidate and
shares the unchanged trace waiter with the full VM test. The four selected source
hashes match the patched-542 worktree tested by
[`e1822efe`](https://app.buildbuddy.io/invocation/e1822efe-6946-4b67-9774-f176cf62939d):
one JUnit case, no failure/error/skip, 307.258 seconds at suite level. This was
before the commit and is not a literal-194 execution or matched speed comparison.
The first candidate attempt failed on an invalid test Identity name before either
guest booted; the corrected suffix uses valid `claude` and `codex` names.

Both real guest boots retain issuer/credential binding, public authenticated
readiness, Claude's private supervision receipt, shared supervised setup and
cleanup fence, tool-mediated workspace work, post-operation identity and every
existing process-trace assertion. The full required VM still owns its substantive
scripted turn/history/admission, guest supervision/cgroup probe, Egress CLI and
Notification CLI assertions. OOM coverage stays separate.

The boot candidate now also executes the existing Egress Identity and successful
Notification CLI reads against the deployed services inside its already-required
workspace Shell turn. It requires the exact deployed Identity, a successful
Notification read, both original workspace operations and post-operation Pod/SVID
identity, with no extra model turns. This later extension passes in
[`55e99bb9`](https://app.buildbuddy.io/invocation/55e99bb9-23f1-47fd-9217-b6e2fc6c82ea):
one fresh test, 255.6 seconds at target level, 291.939 seconds for the invocation.
It closes the deployed CLI-routing ownership gap; the direct guests separately
require a nonempty seeded Notification row and old-SVID revocation.

The current full VM remains required until that ownership transfer and the
whole selected graph qualify. All three required checks for the
witness-only commit `1948106` passed; Bazel CI completed at 22:10:16 UTC.
These later platform changes are outside those checks.
