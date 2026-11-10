# VM executor replacement during the Codex tool OOM test (2026-11-02)

PR [#13769](https://github.com/agentydragon/ducktape/pull/13769), head
`d9ec981bfb79b58559345ea3798ac19033fdc181`, failed `//agentplane/system_tests/suites:test_vm`
in [BuildBuddy invocation `0bffd229-a7da-53ab-b0a5-ce614364b378`](https://app.buildbuddy.io/invocation/0bffd229-a7da-53ab-b0a5-ce614364b378).
BuildBuddy records `ea51837a1eafa025ef0aa4e4b2c7036a977c9665`, GitHub's synthetic
merge of base `f0843d4b632608daf52b76287a6318e18d6b7142` and that PR head.
The body failed in `codex_tool_oom` at `assert after.cgroup == before.cgroup`;
this was separate from the boot-evidence teardown failures seen on other runs.

## Retained evidence

- `test.outputs/vm-codex-oom.json` records the before cgroup as
  `.../agentplane-native-70fe4dee77b44f4d-e71df7f4-4108-47a1-bec7-f776c6cb9975.service`
  and the follow-up cgroup as
  `.../agentplane-native-70fe4dee77b44f4d-d0c45bfc-12d2-407c-b995-9eb28a89c416.service`.
  Both are under `agentplane-executors.slice`. The observer reported a 96 MiB
  limit and `memory.oom.group=0` for both; the new leaf's `oom_kill=0` cannot
  prove what happened to the old leaf. The executor slice's hierarchical `oom`
  counter advanced from 0 to 8.
- Before and immediately after the OOM command, the same JSON records Runtime
  boot ID `df02fb7d-616c-409d-82fd-c831653d3266`, Codex harness PID 738,
  harness process ID `38d2802f-23f7-4880-8d83-05ad2ed5a5ab`, Session ID
  `6ef2bfa2-f557-493f-97d0-21182b90451e`, and native Session ID
  `01a19b51-e164-7531-87f8-a0abe22c7781`. The OOM tool returned 137; a
  same-Session follow-up returned 0 and printed `VM_OOM_FOLLOWUP_OK`.
- The retained OOM console at
  `test.outputs/vm-artifacts/consoles/systest_virt-launcher-systest-vm-codex-oom-7ps57_1b1ccbce-d13b-4fbf-a60a-945b6490e0d2/0.log-1158834.cri.log`
  reports a kernel memcg OOM at guest monotonic 222.9 s. Its victim was `awk`
  PID 1484 in the original executor service. At 231.6 s, Runtime logged
  `embedded Execution backend loss fenced`: an exec-server WebSocket health
  handshake to `127.0.0.1:4501` exceeded its deadline. At 202.8 s, before
  the OOM, Runtime had also logged timed-out systemd observations for the old
  executor service and an unrelated native service.

`MEMORY_PROBE` in [the scenario](../../../system_tests/scenarios/vm_sandbox.py)
reads `/proc/self/cgroup` and selects the observer snapshot for that path. The
paths therefore identify the services that ran the two commands, rather than
an observer choosing the wrong leaf. The [systemd launcher](../supervision/systemd_launcher.go)
uses a stable owner-scope prefix; [each transient service launch](../supervision/systemd.go)
gets a new UUID. The different UUIDs establish executor service replacement.
The [health monitor](../execution/codexexecserver/server.go) probes every five
seconds, with a three-second deadline and three-failure limit. A backend loss
then goes through [embedded Execution fencing](../execution/service/service.go).
The scenario's Runner status checks track the Codex harness and Session, not
this separate embedded Execution service. They establish harness survival,
not executor continuity.

## Later exit-143 attempt (PR #14018)

The same VM test failed on PR [#14018](https://github.com/agentydragon/ducktape/pull/14018),
head `c18e229587b30411ab4fdd7309dfef154f916656`, with guest kernel
`6.12.115-2`. [BuildBuddy invocation
`7b9ee9a0-7ed8-53c9-af83-197e6b7fe6e6`](https://app.buildbuddy.io/invocation/7b9ee9a0-7ed8-53c9-af83-197e6b7fe6e6)
failed `codex_tool_oom`: the test expected the allocating command to exit 137,
but its native Session reported exit 143 and only `VM_OOM_ALLOCATING` output.
The retained `vm-codex-oom.json` contains only the pre-command snapshot: the
unit had a 96 MiB limit, `memory.oom.group=0`, and zero OOM counters. No
post-command cgroup snapshot was recorded. The guest console and journal have
no memcg OOM message for this attempt; that does not prove no OOM event occurred.

The native Session event stream records the command starting at 23:25:08.789Z
and returning exit 143 at 23:25:36.489Z. In between, `systemd[1]` began
stopping the same transient unit at 23:25:32.755Z. It deactivated at
23:25:37.253Z, stopped at 23:25:37.271Z, and reported a 96 MiB peak. Runtime
logged `embedded Execution backend loss fenced` at 23:25:39.065Z. Its error
chain was `execution backend lost` → `execution backend stopped responding`
→ WebSocket dial handshake to `127.0.0.1:4501` failed with
`context deadline exceeded`. The successor unit started at 23:25:39.883Z and
was active at 23:25:41.790Z.

The monitor in [the executor service](../execution/codexexecserver/server.go)
uses a five-second interval, three-second deadline, and three consecutive
failures. It logs only the final error, not each failed probe or its timestamp.
The [native unit configuration](../supervision/systemd.go) sets
`KillMode=control-group` and `KillSignal=15` (`SIGTERM`). The stop request
preceded the command's exit, so the 143 result is strongly consistent with
Runtime stopping the executor unit after health failure and the unit's SIGTERM
reaching the command. It does not establish why the local WebSocket endpoint
stalled.

This failure used the new package, but it does not show whether the package
update caused or prevented the handshake stall. The old-kernel [#13769
attempt](https://app.buildbuddy.io/invocation/0bffd229-a7da-53ab-b0a5-ce614364b378)
ran Linux `6.12.63-1`; its OOM command returned 137, its same-Session follow-up
returned 0 in a different transient unit, and Runtime logged the same class of
health fence after a memcg OOM. That shows the fence can occur on the old pin;
it does not exclude kernel causality for either attempt. Per-probe timestamps
and executor process/socket evidence around the stall would be needed to
identify why the health handshake stopped responding.

## Causal boundary

The retained evidence proves the tool's memcg victim, a later exec-server
health deadline, fencing, and a new executor service handling the follow-up.
It does **not** establish why the local WebSocket handshake became unresponsive
or whether the OOM, guest scheduling, systemd contention, or another mechanism
caused that deadline. The earlier systemd observation timeouts show concurrent
observation trouble but do not identify the handshake cause. No retry of an
uncertain effect or timeout change follows from this record alone. A future
investigation needs per-probe timing and exec-server process/socket evidence
from the interval before the fence, while retaining the separate harness and
executor identities.
