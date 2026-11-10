# VM Codex executor readiness

[CI run 40835085459](https://github.com/agentydragon/ducktape/actions/runs/40835085459)
at `01f147b890` failed `test_vm_mtls_certificate_authenticates_a_session[codex]`.
The other 1,669 tests passed. [BuildBuddy invocation](https://app.buildbuddy.io/invocation/2e1af4b4-2dbe-5b6f-b70d-ccd76e4e76b7)
retains the test log, native History and guest console under `test.outputs/vm-artifacts/`.

## What failed

The executor's first generation began at 07:32:00.536 UTC and was observed running
at 07:32:05.903. It exhausted its 20-second readiness budget and was fenced by
07:32:24.865. The successor started at 07:32:25.868 and became ready at
07:32:32.322. The app-server's separate 30-second dependency deadline expired at
07:32:30.517. History recorded `commandFailed` and `harnessLaunchFailed` before
any model request. The credential test left its collector in a background task,
so the collector's early stream-ended assertion was masked by the scripted model
wait until the 600-second scenario deadline.

The first generation consumed and retried at least 13 failed probes between
07:32:07.291 and 07:32:18.099; several retries were less than 300 milliseconds
apart. It was not stuck waiting for one probe or its completion notification.
The connected-systemd observation timeout at 07:32:18.436 takes the reaper's
retry branch, which does not directly fence the process.

## What remains unknown

The first generation's probe errors were stored only in transient controller
state and were overwritten. Its stdout was discarded, and its retained stderr
contains no executor diagnostic. The launcher used the fixture's documented
QEMU TCG emulation with one vCPU. No retained evidence establishes CPU pressure,
an OOM, a stalled protocol stage, or a healthy executor incorrectly rejected by
the controller. The successor's eventual readiness does not establish that its
predecessor was ready. No executor startup correction is established.

Readiness diagnostics now name the failed protocol stage, retain first/changed
probe errors with the process generation ID, and log the final failure before
fencing. The generic deadline path preserves the last completed probe error.
The test driver reports terminal startup reasons and owns its collector through
a task group. These changes retain evidence and expose failure promptly; they
do not change executor deadlines, restart policy or admission behavior. The
earlier readiness failure's cause remains unknown; a later successful VM run does
not establish it.
