# Bazel client-disconnection fix: matched control

**2026-10-28 UTC:** upstream commit
[29b6ec37](https://github.com/bazelbuild/bazel/commit/29b6ec37e9aff603d4da51570ffb175071605de1)
fixed the observed client-death behavior in one matched pair. With its immediate
parent, killing the actual Bazel client left the original server reporting that
the dead client's command was running. With the fix, an info command completed
on the same original Java server within **0.731 seconds** of the pre-SIGKILL event.

This is a controlled client-death experiment. The outer runner deliberately
remained alive to retain the witness; it does not attribute the hosted
blocked-analysis case or measure its lease. [Selected evidence](bazel_client_disconnect_comparison.json)
retains binary identities, events, receipt and artifact digests, and excluded
setup attempts. The [hosted trace request](host_trace_request.md) remains relevant.

## Matched pair

| Binary                      | Child / outer invocation                                                                                                                                                      | Result after killing only the native client                                                                                                                             |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Immediate parent `8bf014af` | [e37ff6f6](https://app.buildbuddy.io/invocation/e37ff6f6-ba4d-405d-a611-03a899f433df) / [fc8047cf](https://app.buildbuddy.io/invocation/fc8047cf-aba6-4476-a135-dfec0448b35e) | Client exit witnessed; nonblocking info returned 9 and identified dead client PID 9083 as still running. A subsequent blocking info request timed out after 25 seconds. |
| Fix `29b6ec37`              | [29f85810](https://app.buildbuddy.io/invocation/29f85810-a082-4808-b09e-1ca3490bc4bb) / [18d3e690](https://app.buildbuddy.io/invocation/18d3e690-0b9a-4b2c-a0d0-c17bbbb3fa94) | Client exit witnessed; nonblocking and blocking info both succeeded. Server PID 8152 and its process start time were unchanged.                                         |

Both runs used the same standalone workspace, launcher, runner/action images,
`--remote_retries=0`, `--keep_going`, `--nopreemptible`, and uncached 90-second
remote genrule. Each had a fresh child UUID and output base. The controller
required both an executing action and watcher readiness before requesting the
signal. It did not cancel the child or outer invocation until after recording
the outcome. Both effective retry settings and the development versions were
verified in BES.

The native clients were identified by executable byte count and MD5 against the
official prebuilt artifacts selected by Bazelisk. The watcher opened the exact
client/server pidfds before the signal. It checked all launcher threads' child
lists; Go may spawn a child from a thread other than its main thread.

| Case   | Native client / launcher / server PIDs | Official binary bytes / MD5                     |
| ------ | -------------------------------------- | ----------------------------------------------- |
| Parent | 9083 / 9066 / 9085                     | 65,931,761 / `ccb370cb79939f6c666bc745b3a0c9cb` |
| Fixed  | 8150 / 8134 / 8152                     | 65,931,898 / `19e5d274dd8ad9b8a5c904e028be42bc` |

The sizes and checksums came from HTTPS metadata for
[the parent artifact](https://storage.googleapis.com/bazel-builds/artifacts/linux/8bf014af128dbf5b886ed403b115366ef3e57c12/bazel)
and [the fixed artifact](https://storage.googleapis.com/bazel-builds/artifacts/linux/29b6ec37e9aff603d4da51570ffb175071605de1/bazel).
The repository's `.bazelversion` remains 9.2.0; only the disposable workspaces
selected these development commits.

## Retained fixed-run sequence

| Remote event                                         | UTC             |
| ---------------------------------------------------- | --------------- |
| Record actual-client-only SIGKILL before sending it  | 05:38:06.247583 |
| Client pidfd becomes readable                        | 05:38:06.250043 |
| Nonblocking info returns 0                           | 05:38:06.908761 |
| Blocking info returns 0 on unchanged original server | 05:38:06.977740 |
| Watcher begins its own cleanup shutdown              | 05:38:07.548297 |

The measured interval is 0.730139 seconds on one remote monotonic clock,
rounded outward to 0.731 seconds. It includes signal delivery, witness CAS
publication, and the confirming commands, so it is an observed upper bound,
not exact build-stop latency. The server's idle acknowledgment precedes the
watcher's cleanup shutdown. No Java server kill was added to either measured
client-death path.

The parent's retained nonblocking stderr is:

```text
Another command (pid=9083) is running. Exiting immediately.
```

This is positive evidence that its command lock remained held after that
client exited. The later timeout is not, by itself, a measurement of continued
CPU work or physical remote-action execution.

## Fix and release availability

Bazel [issue #30954](https://github.com/bazelbuild/bazel/issues/30954) describes
this killed-client behavior. The fix captures the command thread and interrupts
it once when the client disconnects. The old handler could leave a silent
command unnotified or interrupt a progress-reporting thread instead.

On October 28, the latest release API reported 9.3.1. Direct source inspection
found the old handler in both 9.2.0 and 9.3.1; see the
[release/source evidence](host_trace_access.json). An upgrade to 9.3.1 alone
therefore does not include this change. A targeted backport or a version known
to contain it is the next implementation candidate. These two development
builds have not been validated against the full repository, and this experiment
does not justify restoring general remote retries.

## Rebuilding and limits

Private scripts, raw responses, and per-attempt source snapshots are under
`/tmp/ci-bazel-client-death-fix-2026-10-28/`. Reuse the
[producer-stop witness procedure](producer_stop_witness.md#rebuild-the-probe),
but select the immediate parent and fix with `USE_BAZEL_VERSION` and each
standalone workspace's `.bazelversion`. Bazelisk supports
[prebuilt commit versions](https://github.com/bazelbuild/bazelisk#how-does-bazelisk-know-which-bazel-version-to-run).
Verify the actual executable, preserve the outer runner, and have the remote
supervisor consume a unique CAS control marker before signaling only that
native client. Use readiness conditions; workload and request deadlines bound
resources. Keep credentials out of action inputs and published evidence.

Two earlier setup attempts did not reach the measured signal: one API TLS
handshake timed out before readiness, and one client-discovery implementation
missed children of non-main launcher threads. They are retained as inconclusive
setup attempts, not comparison results. The retry used bounded read-only API
retries, disabled outer CLI retries, and a 180-second remote request timeout.
Final child/outer cleanup requests for both measured runs returned HTTP 200.
The earlier missing-child cleanup returned 500; it is not counted as a
successful cleanup acknowledgment. No physical teardown claim follows from
these responses.

The earlier [producer-stop witness](producer_stop_witness.md) labeled its
launched command PID as `client`; that can be a launcher. Its original-server
info completion still proves the build command finished, but those earlier
pidfds did not independently identify the native client. This comparison adds
that executable check explicitly.
