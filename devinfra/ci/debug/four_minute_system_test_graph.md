# Four-minute system-test feedback: graph constraints

The four-minute **mean** system-feedback objective is not supported by the current
required CI graph. This note models the full-graph Agentplane class, where the
[Nov 12 latency report](https://github.com/agentydragon/ducktape/blob/c342162ca9d66f0be60534a588dcaf137e6e1ef0/latency/index.html)
measured a 10m03s median and 23m30s p90 across green Agentplane service heads.
Its clock is workflow creation to the last visible successful required gate, a
proxy for auto-merge eligibility; it does not measure push-to-eligibility or
agent notification latency. The two physical runs below are examples, not a
distribution or a before/after experiment.

| Fresh Bazel invocation                                                                | Tested source | Elapsed | Observed limiting paths, seconds from invocation start                                                                        |
| ------------------------------------------------------------------------------------- | ------------- | ------: | ----------------------------------------------------------------------------------------------------------------------------- |
| [3f16749b](https://app.buildbuddy.io/invocation/3f16749b-55cb-517e-a66d-39d1e8237f79) | `27331cf`     |   816.4 | Pod 276.7 + 539.4 = 816.1; VM 213.9 + 562.2 = 776.1; MCP services about 203 + 371 = 574; Gateway budget about 272 + 352 = 624 |
| [2e817264](https://app.buildbuddy.io/invocation/2e817264-db20-5c8e-82a1-5faf0bcc781e) | `66379de`     |   571.3 | MCP Actions process 467.8; Pod process 405.1; VM process 293.6; MCP parent delegation process 370.4                           |

The first run needs at least 576 seconds of **combined** reduction on its
last-finishing path to finish by 240 seconds. Accelerating VM alone changes
none of its observed wall time while Pod still ends at 816.1 seconds. The
second run changes the bottleneck to MCP Actions. Reported BuildBuddy
TestRunner queue was about 0.06 seconds for these managed suites; the outer
remote runner's admission and source/image prerequisites are separate clocks.
No provider allocation clock or account entitlement was observed.

A newer current-source Pod control then entered a different queue regime:
its TestRunner action queued at 22:31:16.974 UTC, started on a worker at
22:40:13.796 (536.82 seconds later), executed from 22:40:18.119 to
22:46:24.758 (366.64 seconds), and completed worker output upload at
22:46:27.929. The action's result blob was unavailable when checked, so
these timestamps establish physical work, not its test verdict. The old
0.06-second queue samples cannot be extrapolated to current load; this
single long queue does not identify an account quota or provider capacity
limit. A four-minute design must test admission under comparable concurrent
load as well as shorten the action graph.

The retained [2e817 Pod trace](https://app.buildbuddy.io/invocation/2e817264-db20-5c8e-82a1-5faf0bcc781e)
records 111.01 seconds of stack setup, 430.44 summed seconds across 21
case spans, and 30.25 seconds of cleanup. The longest individual case was
81.03 seconds. If case durations were frozen and contention absent, the
current two-slot batch has a 215.22-second work bound; four slots would have
a 107.61-second bound. Holding that run's setup and cleanup fixed too, even
perfect four-slot scheduling would need at least 248.9 seconds **after the
action starts**. These are conditional arithmetic bounds, not predicted
speedups or universal lower bounds: case spans overlap, some cases have
exclusive ownership, and a different fixture layout can change case,
setup and cleanup durations. An earlier 2→3-slot trial did not show
a complete-CI benefit. The 816-second run's Pod setup alone was 153.02
seconds, so its corresponding bound is worse.

The 2e817 VM trace records 77.23 seconds of setup, then three concurrent
scenarios: Claude 160.38 seconds, Codex 139.16 seconds, and Codex tool OOM
154.66 seconds, followed by 30.65 seconds of cleanup. The required suite
on current `devel` (`9ed77375a9`) now runs the two ordinary turns; [#15897](https://github.com/agentydragon/ducktape/pull/15897)
moved the OOM probe to manual. Claude was already the last scenario in this
older trace, so that trace gives no VM critical-path saving from the move.
Do not propose cgroup isolation as a guaranteed Codex repair. A same-source
KVM pilot measured 262.3 seconds versus 474.5 seconds for managed TCG, but
used different worker resources and cleanup. The earlier, now-closed #15822
placement candidate requested 10 GB, above the existing worker's 7-GiB
advertisement. Current `x/vm_kvm:test_vm_kvm` instead requests 7 GiB on the
four-CPU worker. A required-CI placement change still needs a resource-matched,
capacity-aware pilot under the current suite.

The following **conditional path models** hold the measured 2e817 case work
fixed. The 40-second action start and 50-second warm setup are assumed target
budgets, not observed results. Starting within 40 seconds requires the Bazel
analysis, exact-source image builds/runfiles and remote action admission to
finish within that interval. The newer control's 536.82-second queue shows
that this admission budget is not presently reliable. The models exclude
GitHub admission and outer runner startup, plus extra transfer/scheduling
and changes in contention;
the full gate is the maximum of all required paths, not any one row. Setup
and cleanup can change under the proposed architectures too.

| Pod arrangement                                         | Conditional finish from Bazel start | Interpretation                                                                                            |
| ------------------------------------------------------- | ----------------------------------: | --------------------------------------------------------------------------------------------------------- |
| Current 405.1-second action, moved to a 40-second start |                              445.1s | Early inputs alone miss 240s.                                                                             |
| Two ideal slots, 50s setup, 30.25s cleanup, 40s start   |                     at least 335.5s | Reuse plus early inputs alone still misses.                                                               |
| Four effective slots, same setup/cleanup/start          |                       at least 228s | The Pod path approaches 240s only if the frozen-work assumptions hold; external gates, MCP and VM remain. |

For the VM, moving the measured 262.3-second KVM pilot action to a hypothetical
40-second start still ends at 302.3 seconds. A 50-second setup in place of
its measured 128-second setup would make the arithmetic about 224 seconds,
holding the rest of that action fixed. No such combined run exists; the
earlier 10-GB pilot target is closed, and the current manual target has a
different 7-GiB request. These conditional examples
show why the options must be tested together without crediting their potential
savings as an achieved four-minute CI result.

## Architectural choices and their next test

| Choice                                                                                                | Graph term it changes                                                     | Coverage, cost, and discriminating evidence                                                                                                                                                                                                                                                                                                                                                                                                             |
| ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Narrow per-suite image inputs ([#15214](https://github.com/agentydragon/ducktape/pull/15214))         | Bazel prerequisites and OCI transfer before an action can start           | Keep exact-source service and tool images. The published layer reduction is bytes, not measured wall time. Compare cold and warm physical input-ready and action-start clocks on the same source. No standing capacity.                                                                                                                                                                                                                                 |
| Improve VM/Pod's pre-existing retained K3s baseline                                                   | Repeated import/setup inside each action                                  | These suites already preserve a compatible containerd store; #15188 did not introduce that behavior for them. Keep DB, PKI, workspaces, namespaces and scenario resources fresh. Measure reset/import spans, hit rate and last gate on matched inputs; a cold miss must still pass.                                                                                                                                                                     |
| Firecracker image-store reuse ([#15188](https://github.com/agentydragon/ducktape/pull/15188), merged) | Firecracker's previously non-persisted image store                        | Its cold/warm, fresh-UID and forced-reset checks establish safety of this separate path, not a Pod/VM speedup. Measure Firecracker physical action and its contribution to the last gate; no standing pool cost.                                                                                                                                                                                                                                        |
| Distribute independent Pod/MCP cases across separately owned stacks                                   | Case work bound and late tails, at the price of repeated stack setup      | Preserve every assertion and global-mutation isolation. The Pod batch currently uses two slots and an exclusive storage case; MCP Actions already batches three cases at two slots while OAuth linkage stays serial. Split targets need their own fresh fixture and additional 4-CU actions; measure case critical paths, duplicate setup, aggregate CU-minutes and final gate. The current Pod-tail experiment is only a bounded test of this premise. |
| Place the VM suite on eligible KVM workers                                                            | VM scenario execution, particularly TCG guest time                        | Keep real KubeVirt, systemd and kernel checks. Provision enough memory, one action per worker, admission/drain ownership and matched cleanup; measure queue and full-graph end time under concurrent load. The existing pilot cannot be credited as a production CI saving.                                                                                                                                                                             |
| Prebuild or retain a complete mutable test environment                                                | Multiple setup terms, potentially before source-specific action admission | Requires versioned image/CRD/PKI identity, fresh per-test DB and credentials, cleanup/reset after crash, and protection from cross-PR contamination. A standing pool adds idle compute and maintenance. First prove invalidation and fresh-state equivalence; no latency estimate is defensible yet.                                                                                                                                                    |

For a 240-second full-graph gate, a conditional **budget**, not a forecast, is
to start every limiting action within 40 seconds and finish each within the
remaining 200. On the slower run's frozen durations this requires at least
339 seconds off Pod, 362 seconds off VM and 171 seconds off MCP services;
on the other run MCP Actions needs at least 268 seconds off its 467.8-second
process. Starting early alone, retaining setup alone, or changing only VM
placement fails this budget. Moving Pod from two to four ideal slots while
retaining the 2e817 setup and cleanup also fails it. A plausible experiment
must combine earlier exact-input readiness, safer fixture reuse and case
distribution, then remeasure all required gates and compute cost. The
combined design is still unvalidated; the next bottleneck may be MCP Actions,
VM, another suite, the Bazel build/lint phase or GitHub's required checks.

The current-source [Pod control](https://app.buildbuddy.io/invocation/88924b1e-808c-4da9-89ec-3ac634268d6c)
uses `630fd860` with an uncached test request. Its outer `bbr` stream later
closed with `Internal: stream closed with task still claimed` after buffered
TestRunner progress reached 592 seconds. The accepted outer execution later
returned terminal status code 13 with that message and no successful
ExecuteResponse. Both outer and inner invocations became `DISCONNECTED`;
the inner physical TestRunner action has completion clocks but its result
blob cannot be read. Neither a pass nor a Bazel gate verdict can be inferred.
This is remote observation/recovery overhead, separate from
the physical critical-path timings above. A separate [Pod-tail split](https://app.buildbuddy.io/invocation/34d911a5-ebfa-4040-b7d7-b59ccd4880ec)
uses that source plus a local patch, with both selected targets uncached. They
were submitted at 22:30 and 22:39 UTC on Nov 12 and overlap. The split's main
action failed before collection because a pytest `-k` exclusion was passed as
two argv tokens; that failure is not workload timing. The TLS-only action
continued, and a corrected [main action](https://app.buildbuddy.io/invocation/fa944edd-c4a0-4c18-b576-1f0ac09d5e58)
was submitted at 22:42 UTC. Its outer observer also lost the stream with
the same error while the physical action was still executing. Their overlap,
separate invocations and different action counts prevent an isolated causal
speedup claim even after they finish. The older
trace showed the security tail ending about 67.9 seconds after all other Pod
cases; a second stack can remove at most that observed post-setup tail if it
finishes in time, while repeated setup or resource contention can erase it.

The split's TLS-only action subsequently passed uncached in 188.77 seconds;
JUnit records exactly the intended security case. Its physical spans were
101.57 seconds of setup, 22.16 seconds of case work, and 29.59 seconds of
cleanup. The older run's corresponding case took about 91.7 seconds, so the
case duration is unstable across these runs. The corrected main action later
queued for about 0.064 seconds and executed for about 388.2 seconds, but its
test verdict was not yet recovered at this refresh. Compared with the control's
366.64 physical execution seconds, this is an uncontrolled process-time
comparison across overlapping, differently selected actions. The TLS result
establishes that the split case can pass in its own fresh stack, not a
combined gate or causal latency reduction.
The exact corrected spike is retained as a
[patch](four_minute_system_test_graph.pod_split.patch), SHA256
`f2078c91e574770b5be108aea1ada5ce03b6d404dcbf4aa9f925066a85b2bc80`,
against `630fd860`. It adds an `args` passthrough to `k3s_suite`, leaves the
main Pod target with 20 selected cases, and adds one manual TLS-only target.
The initial failed action used an unquoted `-k` expression; the retained patch
quotes that expression as one pytest argument. It is an experiment, not a
proposed required-CI change.

Evidence sources: the canonical report's
[`evidence.json`](https://github.com/agentydragon/ducktape/blob/c342162ca9d66f0be60534a588dcaf137e6e1ef0/latency/evidence.json),
BuildBuddy physical TestRunner records and retained `test-spans.jsonl` for
Pod/VM, [suite input declaration](../../../agentplane/system_tests/k3s/defs.bzl),
[Pod batch](../../../agentplane/system_tests/suites/test_pod.py),
[VM batch](../../../agentplane/system_tests/suites/test_vm.py), and
[MCP Action batch](../../../agentplane/system_tests/suites/mcp/test_actions.py).
The 2e817 MCP trace blobs were advertised but returned 404 when fetched;
there is no per-case MCP attribution here. The historical report's source
predates current `devel`; its timings cannot be attributed to #15188 or #15897.
