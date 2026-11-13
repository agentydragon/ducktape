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

These cases do not prove actual Dispatcher loss/retry of a real Runtime admission
reply, History crash recovery or database failover. Runtime journal/RPC retry,
Gateway stale-incarnation, Dispatcher authorization and History ordering owner
tests retain those narrower guarantees. The full composed coverage audit remains
an adoption prerequisite.

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

Follow [#16099](https://github.com/agentydragon/ducktape/issues/16099) for the
remaining coverage assignments, 30 pinned base/head pairs, busy-window and
four-PR-burst exposure, and rerun-inclusive exact-head required feedback. The
100 native case exposures do not satisfy the whole adoption protocol by themselves.
Stop dedicated optimization at the accepted quarter-payback/two-failed-probe rule;
the four-minute target is an intermediate budget. Image split #15214 remains held.
