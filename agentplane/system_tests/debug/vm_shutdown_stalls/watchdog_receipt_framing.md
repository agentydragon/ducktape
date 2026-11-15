# Shutdown watchdog receipt framing

The Policy manifest omission PR #16699 failed `//agentplane/system_tests/suites:test_vm`
in [invocation ea43cb21](https://app.buildbuddy.io/invocation/ea43cb21-f985-581b-9835-261a03818329).
Both Claude and Codex scenarios passed; fixture teardown raised `GuestShutdownError` for VM 2.
This is a console framing failure, rather than evidence that the guest stalled.

The independent raw QEMU serial file and CRI console agree:

- Both `/state/workspaces` and `/state` proof checks report `findmnt_status=1`.
- The next record is `do_epoll_waitAGENTPLANE_GUEST_SHUTDOWN_COMPLETE`.
- `AGENTPLANE_SHUTDOWN_PROOF_EXIT status=0` follows it.
- PID 1 reports successful completion of the proof service and reaches poweroff.
- The guest ends with `reboot: Power down` at guest uptime 143.068257 seconds.

The CRI record containing the joined receipt was written at 12:38:16.533432431 UTC on
2026-11-15. The watchdog printed `AGENTPLANE_SHUTDOWN_PID1_wchan`, then directly copied
`/proc/1/wchan` to its console. That proc file contains no newline. Before the watchdog's
separate `echo` could terminate the value, the proof process wrote its completion receipt.
The strict receipt parser correctly refused the joined record. The child-process observer
has the same unterminated-field write pattern.

Collect each proc value before publishing it with its newline. Keep exact receipt parsing,
shutdown ordering, sampling cadence and deadlines unchanged. A deterministic regression
holds a proc reader alive after it produces an unterminated value and injects a concurrent
receipt on the shared console. The receipt and proc value must each remain separate lines.

The target-history query recorded in the investigation showed devel passes at `b9cae0d134`
(invocation `35e28298-877a-5e4c-b606-32c5e7cf6ee0`), `d06d669cd4`
(`1d9643ec-988b-5e21-8ce6-faca919cd8c1`) and `37c8394bde`
(`b9fc0009-7941-57f8-ba6b-a1fd19a9afc3`). Those passes do not disprove the console race.
No unchanged run was requested to replace the failure.
