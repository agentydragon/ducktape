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
