# Native contracts and deployment graph selectivity

Architecture work remains on [draft PR #16102](https://github.com/agentydragon/ducktape/pull/16102),
branch `agent/test-architecture-pivot`. The reviewed measurements live in the
[architecture report](https://agentydragon.github.io/ducktape/agentplane-test-architecture/).
No required scenario or test frequency was removed.
Independent fixes applicable to devel may be backported separately.

## Native recovery

Commit `d2508cbdd86b1be1ec186cb2e4656857c2be4129` expands the manual
`//agentplane/x/native_contracts:test_recovery` to four cases. Production
Gateway/Dispatcher/History processes, PostgreSQL, envtest and the real Runtime
PID1 container cover completed-turn outage, direct Runtime admission/deduplication,
active-turn SIGKILL/recovery/next input, and History process unavailability/cursor
resumption. The reusable fixture is app-independent. Claude is real; the model is
scripted. Controller statuses are authored; real deployment witnesses remain.

Reproduce fresh exposure with the full published SHA:

```sh
bbr --run_from_commit=d2508cbdd86b1be1ec186cb2e4656857c2be4129 \
  --run_from_branch=agent/test-architecture-pivot test \
  //agentplane/x/native_contracts:test_recovery \
  --nocache_test_results --runs_per_test=25 --flaky_test_attempts=1 --jobs=4
```

[Invocation 3fdecfa7](https://app.buildbuddy.io/invocation/3fdecfa7-65a5-42a4-93d5-da9bc9e49036)
has 25 distinct successful TestRunner actions and 25 four-case JUnit results:
100 case exposures, 25 per contract, no skips or retries. Shared setup makes these
correlated exposures, not 100 independent reliability trials.

The discarded-journal control
[8d8188ad](https://app.buildbuddy.io/invocation/8d8188ad-12a7-42f7-96fa-0409fbaf2236)
fails the retained-Session assertion. A temporary mutation disabling the journal's
command collision guard fails the direct admission case with `DID NOT RAISE
ServiceError` in [740efe9c](https://app.buildbuddy.io/invocation/740efe9c-93d1-433b-b7e1-fafe5c40da75).
That mutation was restored and never committed. The latter invocation used a
patched tree; do not describe it as an unmodified commit execution.

These four cases do not prove actual Dispatcher loss/retry of a real Runtime admission
reply; the separate contract below covers a post-handler error. They do not prove
History crash recovery or database failover. Runtime journal/RPC retry,
Gateway stale-incarnation, Dispatcher authorization and History ordering owner
tests retain those narrower guarantees. The full composed coverage audit remains
an adoption prerequisite.

The separate manual `test_admission_reply_loss` uses the same production Runtime
and CLI startup code with a test-only `ControlInterceptor`, after existing
authorization and fencing. One actual Dispatcher submission reaches the real
Runtime handler, commits an admission, then receives an injected `UNAVAILABLE`.
The actual retry reaches the handler again and returns the same journal receipt.
Both intermediate transports disable retries, so the repeated handler call
belongs to Dispatcher delivery. The test compares both full responses with
Dispatcher's receipt and History's one admission, retains the request/result
records in undeclared outputs, checks that later Dispatcher receipt replay
causes no third handler call, and completes a distinct next input.

The patched-tree positive
[0b289ddb](https://app.buildbuddy.io/invocation/0b289ddb-c9ff-4c99-8e0b-c29abcefd284)
passes. A temporary one-line mutation rejecting valid identical-command journal
replay as a conflict fails in
[b10c10d5](https://app.buildbuddy.io/invocation/b10c10d5-74cd-4e1b-b738-28e9c68fdd94):
the second real handler returns `AlreadyExists` without an admission. The
mutation was restored; it is not part of the branch. The existing four-case
target then passes in
[b2712fa6](https://app.buildbuddy.io/invocation/b2712fa6-0eee-40a2-87cf-818ad4b0bf8c).
These calls used the working-tree patch over d717, not an unmodified published
revision. This establishes the composed post-handler-error path; it does not
establish HTTP/2 socket-reset behavior or a complete-CI saving.

## Image ownership and the selected graph

Commit `220e6a5a732656f6359234b99d92ae8635f77a6b` adds two manual targets sharing
the unchanged seven `test_services.py` case bodies: MCP (3) and central services
(4). The central fixture retains App, Actions, backend, sampler, controllers, PKI
and PostgreSQL, without MCP edge or external OAuth fixtures. Its unused app
Connection routes report the missing Connection service; it does not cover them.
The required seven-case target remains intact.

Configured closure queries prove the three-node MCP-edge image path exists for
the required target ([aff9fdf1](https://app.buildbuddy.io/invocation/aff9fdf1-e96b-4638-b6c3-6459f23409e0))
and is absent for the central candidate
([a38fb697](https://app.buildbuddy.io/invocation/a38fb697-316d-42cb-b61e-1036769dc1c8)).
This proves dependency removal, not bytes saved or quicker PR feedback.

The pinned A/B cohort runs the original or both candidates alongside unchanged
Pod and VM targets, with uncached test results and one allowed attempt. A1
[3a80aeab](https://app.buildbuddy.io/invocation/3a80aeab-436c-4722-b3b7-5c1f88e2470d)
and B1 [58400d80](https://app.buildbuddy.io/invocation/58400d80-c37c-42b1-be89-1b4ae905f562)
passed the exact MCP 7 versus disjoint 3+4 union, Pod 21 and VM 1 JUnit IDs.
The split duplicates setup and increases aggregate MCP CPU; it has no established
complete-feedback saving. These are selected-cohort probes, not the actual full
diff-selected PR gate. Load differed between arms. The report preserves the
reverse-order repeats and all final outcomes separately.

Commit `4c7731b1be578c9833c570a0d6c029f3b8bcc7f9` also removes the unused MCP-edge
deployment/image from the existing VM stack. Every retained rendered object and
the full VM scenario body are unchanged. The profile now has its actual name,
`NO_MCP_EDGE`, shared by Pod, central-services and VM. No external Connection is
used by the VM cases. App/Actions, both real harnesses, systemd, cgroups, egress,
notifications and SVID continuity remain. Default/MCP/Firecracker profiles retain
their previous image ownership. Verify the full VM scenario and configured closure
on that exact commit; the report records terminal evidence rather than assuming it.
The unchanged full VM scenario passed on the literal commit in
[4aec403d](https://app.buildbuddy.io/invocation/4aec403d-812b-4367-8122-7c2385f0c068).

The separate manual `test_vm_boot_contract_candidate` at
`9bce0a401dc57dba1d98be472462c20e50a1ea91` passed in
[af44c062](https://app.buildbuddy.io/invocation/af44c062-1628-424d-bc69-fdeb73bb0243).
It covers real guest readiness, issued SVID and authenticated typed app → Gateway
→ Runtime status. It has no harness turn and does not replace full VM acceptance.

## Reliable results and future replay

Nested pytest fail/skip/xfail outcomes inside `BaseExceptionGroup` previously
escaped the case-result path and left waiters pending. Focused
[red](https://app.buildbuddy.io/invocation/0b745d32-8c64-4e41-8272-cb3a57023d2f)
and [green](https://app.buildbuddy.io/invocation/1eff1c43-9784-4782-b26f-29ec1bf5c316)
controls reproduce this. The driver now delivers handled groups and wakes all
waiters before propagating an unexpected group, preserving its full leaves.
Cancellation still propagates and drains cleanup. The final CaseBatch and
rendering integration passes in
[7d522bb2](https://app.buildbuddy.io/invocation/7d522bb2-34bb-491a-bd9d-7f2dd0110991).
No production recurrence-rate improvement has been measured.

CI now includes the existing query's selected test labels and source/merge/base
provenance in its existing best-effort runner-probe archive. The labels are
explicitly **before runtime tag filtering**, not all executed TestRunners.
Download through the logged `CI_VM_PROBE_CAS_DOWNLOAD` command; keep raw runner
diagnostics private. Snapshot-write and upload failures preserve both passing and
failing gate verdicts. The no-affected-target early exit has no label snapshot.
No new query, selector policy, scheduled matrix or gate-frequency code was added.
The [ordinary CI archive round trip](https://github.com/agentydragon/ducktape/actions/runs/43398508201)
is verified: the selected-label snapshot matches tested merge
`488adf369a932ae05457808dd0498083f5ce31a1`, actual base
`6787057f85d482979f7c3af5464a401b110dc7d2`, PR head
`7d46adc2b16dfaefdb462f3529af1b41324ad5f4` and test/build invocation identities.

## Failures retained by the reversed-order screen

B2 [e9849ed3](https://app.buildbuddy.io/invocation/e9849ed3-da32-4372-b983-89269ab2ec4f)
has four green final target summaries but five physical TestRunner executions.
Its first VM action failed while pausing BuildBuddy's Firecracker VM: the local
control socket refused the connection (REAPI code 14). A same-digest action then
passed. The underlying executor failure is not visible in the available artifacts.

A2 [d4f6afa5](https://app.buildbuddy.io/invocation/d4f6afa5-68e5-480b-b923-486ef3004ed8)
passed both VM scenario bodies, then failed stack teardown. The background
block-storage worker had died on a read-only PVC LIST deadline; `worker.result()`
propagated that error from `resources.close()`. The API-side delay is unknown.
The same teardown signature appears in
[`vm_shutdown_stalls/five_run_full_suite_control.md`](vm_shutdown_stalls/five_run_full_suite_control.md).
Neither failure is erased by the later green no-edge VM run or ordinary CI.

The provisioner now retries only its read-only PVC LIST for deadline/connection/
payload transport failures: three attempts, unchanged 10-second request timeout,
0.1-second backoff and warning diagnostics. It starts no new LIST after stop;
cancellation, authorization errors and exhausted retries still propagate.
StorageClass/PV creation and loop attachment stay outside retry. Baseline
certification still independently verifies namespace/PV cleanup before reuse.
The [seeded real-apiserver red control](https://app.buildbuddy.io/invocation/66f569aa-3cf9-4ba7-a0c1-93bd0a4d7806)
reproduces the worker/teardown failure. The
[six-case positive/control-flow target](https://app.buildbuddy.io/invocation/0ba70772-164b-45d4-9821-f994a8857836)
passes with the same two PVs and loop attachments after deadline/connection
recovery, exact last-error propagation on exhaustion, stop, cancellation and
403 checks. This proves the injected-failure behavior; the real API delay's
cause and recurrence rate remain unmeasured.

## Required-head capacity evidence

All three required checks passed for head
`d717b4256d63c540179e61b5b90ed8c7d25f64d4` in
[workflow 43403429849](https://github.com/agentydragon/ducktape/actions/runs/43403429849):
12m01s from workflow creation to the last required result. Its tested merge is
`c22d0c4e37d9abe82de088c94e835466a3dd117d`, with actual base
`a8f15395e09d8d2edac60ca810d17bef7cc2e225`. This is one full-sweep head,
not a matched routine-backend comparison. The actual selected-label archive
contains 2,223 labels before runtime tag filtering; its test invocation has
87 new physical TestRunner records and no failed physical record.

In [that test invocation](https://app.buildbuddy.io/invocation/d91c70d4-3460-54bc-94a0-164f963b0882),
the Firecracker action queued for 310.328s, prepared for 0.830s and executed
for 213.737s. Decoded Action properties establish self-hosted `kvm-vm`, Docker,
4 CPUs, 6 GB and 20 GB disk. Mimir query-range samples every 15s from
09:44:30 to 09:49:15 UTC on November 13 show three desired, registered and
up workers, each with 4,000 assigned milliCPU, at the configured maximum of
three one-action workers. Relevant gauge ages were checked: maximum 50.521s.
Assigned CPU is an allocation, not measured utilization; local queue reservations
can duplicate a central task and do not count distinct queued PRs.

This window establishes saturated compatible capacity. Raising only the idle
floor would not remove its wait. Removing this queue with every other action
unchanged has at most 153.499s of observed test-tail headroom: the VM target
would become last at 09:50:29.091 UTC. This is a conditional bound, not a measured
feedback saving. Do not route additional VM tests into this pool without a
capacity comparison. Burst qualification must compare narrower validated KVM
ownership with bounded horizontal capacity, accounting for cold registration,
cache, host/SSD eligibility and recurring reservations. No live policy changed.

The unchanged full VM scenario also passes on literal d717 in
[1249aacf](https://app.buildbuddy.io/invocation/1249aacf-2eab-492c-97a2-eb27403ef13e):
one physical action, 379.284s process and 359.159s JUnit suite. Independent devel
backports [#16198](https://github.com/agentydragon/ducktape/pull/16198) (CaseBatch)
and [#16201](https://github.com/agentydragon/ducktape/pull/16201) (PVC reads) pass
all required checks and are merged as of 10:16:56 UTC on November 13.
Start post-merge recurrence measurements from each merge; the focused controls
do not establish a lower real failure rate.

Follow [#16099](https://github.com/agentydragon/ducktape/issues/16099) for the
remaining coverage assignments, 30 pinned base/head pairs, busy-window and
four-PR-burst exposure, and rerun-inclusive exact-head required feedback. The
100 native case exposures do not satisfy the whole adoption protocol by themselves.
Stop dedicated optimization at the accepted quarter-payback/two-failed-probe rule;
the four-minute target is an intermediate budget. Image split #15214 remains held.

## Direct guest packaging without cluster setup

The manual `//agentplane/x/direct_vm_boot:test_guest_boot` runs the existing
kernel/initramfs, guest root and Runtime disk with the already-pinned KubeVirt
launcher image's QEMU/virtiofsd. A test-only root overlay starts one systemd
probe. It checks PID1, cgroup v2 and seven real mounts, writes a fresh host nonce
through virtiofs and requests poweroff. The host requires that marker, all mount
records, QEMU exit zero and Linux's `reboot: Power down` message. Each action has
fresh state, workspace and read-only input artifacts.

The initial patched-tree healthy run
[dd39c14f](https://app.buildbuddy.io/invocation/dd39c14f-badf-45e0-9b74-3cac57d20364)
passed one case: 61.20s pytest and 69.3s Bazel target time. Its serial trace reaches
poweroff at 17.336s of guest kernel time. These clocks describe different scopes;
there is no matched comparison with the KubeVirt VM scenario. The proof establishes
software-emulated boot feasibility, not a full-PR saving.

Configuration ISOs are empty. The Runtime binary must be executable but is not
configured or required healthy. Controller-issued identity, PKI, scheduling,
KubeVirt, authenticated Runtime operations, harness turns, egress and Kubernetes
storage remain owned by the required deployment tests. The wrong-config-serial
control is opt-in; it must boot Linux/systemd and fail the same readiness assertion
when the real required mount cannot appear. See the experiment README for commands.

## Historical replay input provenance

Thirty distinct recent PR heads now have resolved CI-tested base/head pairs,
spanning Runtime/backend, App/browser, MCP and platform/wiring. The resolved
source SHA in each latest-attempt action log agrees with BuildBuddy commit
metadata; its immutable Git parents identify the historical tested base and
exact PR head. The cohort preserves 40 workflow attempts, including failed and
cancelled outcomes. Current PR API bases and landing commits are separate facts.

This is preparation, not 30 completed graph replays. Earlier attempts retain
statuses and clocks but do not yet have individually resolved tested merges;
latest gate clocks do not imply complete rerun-inclusive feedback for every
historical head. GitHub and BuildBuddy outcomes remain separate, including a
green GitHub head with BuildBuddy `REMOTE_CACHE_EVICTED`. An architecture overlay
must preserve each historical production tree or explicitly record incompatibility.
The curated cohort and its API provenance will accompany the next architecture
report snapshot; raw logs remain private task artifacts.

The cleanup-tested healthy run
[eb0cc3b8](https://app.buildbuddy.io/invocation/eb0cc3b8-7312-4360-bea2-3e14b6a04bd3)
passes one case in one physical TestRunner: 60.512s process, 102.875 CPU seconds,
53.177s pytest. Its actual guest reaches poweroff at 11.514s kernel time.
The wrong-config-serial control
[8388bce1](https://app.buildbuddy.io/invocation/8388bce1-d471-498b-a6ee-202e0496bc12)
boots Linux/systemd, waits for the absent required configuration device and fails
the same readiness assertion without a nonce. The deliberately bounded QEMU
process exits 124 after 30s. Both named Docker containers are removed successfully.
These are patched-d717 executions; final diagnostics-copy ordering was subsequently
built with mypy/Ruff before commit. No Runtime/authentication equivalence is implied.

## Remove a false Firecracker image dependency

The existing Firecracker suite's two unchanged cases exercise live guest
suspend/resume, Runtime/Session continuity, real issued and rotated credentials,
guest egress and rejection of a cluster-CA impostor. Neither uses external
Connections. Its stack now shares the explicit `NO_MCP_EDGE` profile and common
service-image map already used by Pod/VM contracts. The rendered-resource control
compares both VM and Firecracker with their full profiles: every retained object
is identical, and only names belonging to MCP edge disappear.

The patched-tree screen
[85ecece4](https://app.buildbuddy.io/invocation/85ecece4-f355-40f9-9cf1-0c370065ff1d)
passes all 18 rendering cases and both full hardware-backed Firecracker cases,
with no skipped cases. Firecracker's Bazel target time is 244.6s; selected invocation
wall time is 408.884s. This preserves coverage and removes an unused image edge;
it is not a matched timing comparison. The recent 15-head MCP source frame has
no PR confined to MCP-edge implementation/tests. Do not attribute a historical
saving to that class without actual selector evidence: common Action, Gateway,
platform and fixture edits can still legitimately select Firecracker.
