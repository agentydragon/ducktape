# VM Sandbox deletion held after a stopped Host lost its cgroup property

On 2026-11-01, PR #13392's `//agentplane/system_tests/suites:test_vm` failed at
commit `76386f69307d283ff0e37b9643ca0f895257fade` in BuildBuddy invocation
[`97a643b2-254e-57c6-a27a-606300c48771`](https://app.buildbuddy.io/invocation/97a643b2-254e-57c6-a27a-606300c48771).
The `codex-tool-oom` scenario completed its OOM and same-Session recovery assertions;
`test.outputs/vm-codex-oom.json` records `"passed": true`. The case timed out in
Sandbox cleanup. Its trace accepted suspend at 03:09:51.487 UTC, observed
`Suspended` at 03:09:51.526, and accepted delete at 03:09:51.542. It did not
observe deletion before the pytest timeout at 03:14:30. The four other VM
cleanup spans completed in 28–33 seconds.

The process spans place the held boundary at Runner Quiesce. The Gateway saw
`draining` for `systest-vm-codex-oom` at 03:09:51.584, then no `flushing` or
`quiesced`. Controller retries at 03:10:51, 03:11:51, 03:12:51, and 03:13:51
received gRPC `UNAVAILABLE`. The guest console shows the first Runtime exit at
03:10:03: Harness Host rejected lifecycle generation 3 because native process
cleanup timed out. Starting at 03:10:08, each Runtime restart failed while
fencing the old `agentplane-harness-host.service` invocation: `systemd cgroup
path is required to prove an empty process domain`. The VM and launcher Pod
were still Running at teardown. The fixture's `/dev/loop4` detach error followed
from that still attached VM; the History `NOT_FOUND` was an earlier polling result.

`startSystemdUnit` fences the previous Host before starting a new one. The
previous Runtime cannot carry its recorded cgroup path across its own restart.
The error proves that the startup fence reached `systemdCgroupEmpty` without a
path; the artifacts did not capture the complete D-Bus property snapshot.
For a stopped systemd service, `ControlGroup` can be empty while the prior
`InvocationID` remains. In that state the fence correctly refuses to treat the
unit as never started, but cannot recover from the empty path. The candidate
derives a path only for the two packaged units
whose service definitions set `Slice=agentplane.slice`, after verifying the
reported unit identity, stopped state, prior invocation, slice, and zero main
PID. It still checks `cgroup.events` for remaining processes and rechecks the
invocation and state before acknowledging the derived-path fence. Existing
known-cgroup and exact-invocation success checks are unchanged.

The cause of the initial native cleanup deadline remains separate and
unproven. A successful VM suite run after this change must be checked for
whether the recovery branch actually ran; a normal pass alone does not
reproduce the stopped-Host state observed in the failed invocation.
