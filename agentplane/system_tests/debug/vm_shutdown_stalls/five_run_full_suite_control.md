# Five uncached full-suite controls

On 2026-11-06, [`//agentplane/system_tests/suites:test_vm`](https://app.buildbuddy.io/invocation/f2fb8959-2613-400e-a49e-4be88782db1f)
ran five times from source `c9c703f830462bbadec825c8643554903d459b2c`. The command was
`bbr test //agentplane/system_tests/suites:test_vm --runs_per_test=5 --flaky_test_attempts=1 --nocache_test_results --test_output=errors --jobs=1`.
This used the normal VM test configuration, with one attempt per run and no boot-debug or
opt-in libvirt logging changes.

| Run | Result | Test attempt |
| --: | :----- | -----------: |
|   1 | Passed |    490.277 s |
|   2 | Failed |    657.181 s |
|   3 | Passed |    317.005 s |
|   4 | Passed |    312.414 s |
|   5 | Passed |    356.695 s |

All fifteen retained raw serial artifacts contain `AGENTPLANE_GUEST_SHUTDOWN_COMPLETE`,
`All filesystems unmounted.`, ACPI entry to S5 and `reboot: Power down`. Run 2's
UID-correlated launcher logs also show `Signaled vmi shutdown`, libvirt `shutdown` and
`stopped detail="shutdown"` events, and monitor `reason=shutdown`. The same-run handler
artifact has no grace-expiry event for those three VMI UIDs; its two grace-expiry records
belong to older, different UIDs.

Run 2 failed in the test/control path: an exec-server WebSocket disconnected with
`Connection reset without closing handshake` and an `unknown session id` while checking
the VM scenario report. Fixture teardown separately raised `TimeoutError` while listing
PVCs. The guest and host artifacts show orderly shutdown for all three VMs, so this is not
the target post-ACPI shutdown stall.

This single five-run batch did not reproduce the observed late shutdown stall. It is a
bounded negative control, not evidence that the stall is fixed or that any cause is
excluded.
