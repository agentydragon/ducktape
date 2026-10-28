# VM shutdown probe, 2026-10-27

The minimal CI guest does not respond to KubeVirt's graceful shutdown request. KubeVirt waits for
its grace period, destroys the domain, then removes the launcher. The observed cleanup tail is
mostly this shutdown path; changing Sandbox finalizer or live-update sequencing would not remove it.

## Evidence

Baseline source: `51542844ae8d9d18e71cab407b288ec309f80cbf`. The only experimental changes retained
all launcher containers' and KubeVirt components' CRI logs and collected all Kubernetes Events.
Guest behavior, grace periods, cleanup waits and assertions were unchanged.

Command (the duplicate `-k` was harmless; pytest used the last expression):

```sh
bbr test //agentplane/system_tests:test_vm_sandbox_vm \
  --test_arg=-k --test_arg=test_a_vm_sandbox_runs_a_turn \
  --test_arg=-k --test_arg='test_a_vm_sandbox_runs_a_turn and claude' \
  --nocache_test_results --test_output=errors
```

[Invocation](https://app.buildbuddy.io/invocation/c7d1affd-a5eb-44f0-b09b-bf63ae1dcae4).
The CLI retried after a log-stream `RST_STREAM INTERNAL_ERROR`, reusing the Bazel invocation ID.
The first outer invocation was `1a92557f-ddde-49f6-b592-39f786569728`; the retry's outer invocation
was `5e99e4f5-636f-46a9-a5be-b91d3859ca04`. These are not independent manual submissions.

The timeline below comes specifically from the first TestRunner execution:

```text
/uploads/72743d07-6ef7-47da-8169-0e59bd9e0b6f/blobs/e32daf57f785d14ee6b1e96c0becd2d9dd0c435df06a4048218246c15be7253e/536
```

Its action ran 23:23:16.122–23:27:23.016 UTC. Retained XML records one Claude case, zero failures
or errors, 217.366 seconds of pytest time. Its output Tree digest is
`bab3b6e03ce0ffa27a3170e21c050071f60903a55ee49789ed0cccdd097bff05/5120`.
Retrieve it through `bb execution get <execution-id> --output=json` even if retrying the invocation
has replaced its target index.

| Event                                      | UTC             |
| ------------------------------------------ | --------------- |
| Suspend accepted                           | 23:26:31.439532 |
| Effective SUSPENDED mode observed          | 23:26:31.597085 |
| Delete accepted                            | 23:26:31.623207 |
| First graceful shutdown signal, launcher   | 23:26:32.481996 |
| Grace period expired, handler              | 23:27:02.519316 |
| Domain stopped; kill acknowledged          | 23:27:02.743005 |
| Domain lifecycle reports stopped/destroyed | 23:27:02.743211 |
| virt-launcher exits                        | 23:27:03.090782 |
| virt-launcher-monitor exits                | 23:27:10.908892 |
| Sandbox deletion observed                  | 23:27:12.085956 |

Delete acknowledgment to absence: **40.463 seconds**. The graceful-request-to-expiry interval is
**30.037 seconds**; killed-domain-to-Sandbox-absence is **9.343 seconds**. The latter is still real
launcher cleanup, not established as another optimizable delay. Requests recur about every five
seconds until expiry. Effective SUSPENDED status precedes actual guest shutdown.

Earlier invocation `62c582d6-acc0-4f2f-9820-e4ed3f6440c9` showed deletion tails of 40.767, 41.035
and 41.324 seconds across the three VM cases. Its consoles still emitted runner gateway retries
28–31 seconds after delete admission; host launcher-volume unmount started around +40 seconds,
and Sandbox absence followed within one second. Those artifacts lacked handler/compute logs,
which is why the additional probe was necessary.

## Guest defect and intervention

`guest/boot_files.py` includes only `virtio_blk`, `virtio_net`, `isofs`, `erofs` and `overlay`.
The pinned Debian cloud kernel (`35da0f2338242760a9dcde72340bf063601c1a8507402ddf3e88bf154fbc1d06`)
has `CONFIG_ACPI=y`, but `CONFIG_ACPI_BUTTON=m` and `CONFIG_INPUT_EVDEV=m`. The `button` and `evdev`
modules exist in the package and are absent from the generated initramfs. There is no udev/module
autoload in this guest. Its shell PID1 also has no ACPI listener or shutdown sequence.

The pinned Debian BusyBox static package, version `1:1.37.0-6+b5`, includes `acpid`, `poweroff`,
`init`, `ash`, `sync` and `umount`; no additional package is needed. Package SHA256:
`c3515c5b108f5af20ab8d80e05d1c2c60ad0b17fde3615cffb296a2d3fc4347e`.

The guest shutdown implementation follows these steps:

1. Package/load `button` and `evdev`, and verify the actual power-button input device. Do not assume
   it is `event0`; the guest already has a keyboard.
2. Install an ACPI action and start BusyBox `acpid`, forwarding power-button events to PID1.
3. PID1 marks shutdown once, stops runner restarts, signals its tracked runner child with SIGTERM,
   and waits for the child's actual exit. A signal-interrupted shell `wait` is not drain completion.
4. Stop/reap owned helpers, sync, unmount the workspace before `/state`, then power off.

Runner SIGTERM already invokes `Runner.Close()`, which stops native processes and closes journals
before releasing state ownership. Preserve that sequencing. Normal BusyBox `poweroff` only sends
SIGUSR2 to PID1, so the current shell cannot use it as a shutdown supervisor. The final kernel
poweroff call must follow successful drain/unmount; it must not replace them. Make repeated ACPI
events idempotent, and log drain/unmount failures rather than silently bypass them.

Keep the existing external grace period and wait for actual Sandbox deletion. Production's NixOS
systemd guest is a different image; its service stop timeout does not govern this minimal CI guest.

Validation for a candidate: retain these lifecycle logs; observe guest power-button reception,
runner exit and orderly guest poweroff; verify that grace-expiry/domain-destroy messages disappear;
run both harness cases and the Codex OOM case, plus suspend/resume and storage preservation tests.
The baseline probe establishes the failure, not an end-to-end savings measurement. Successful VM
stack teardown now requires the exact guest shutdown-complete console payload for each retained
launcher incarnation. The marker follows runner exit and both successful unmounts; existing test
failures retain diagnostics without adding another teardown assertion. Launcher compute and
virt-handler logs are retained alongside consoles to distinguish orderly shutdown from a forced kill.

## Journal close after ACPI reception

Adding ACPI handling alone exposes a second shutdown dependency. With the correct action installed
at `/etc/acpi/poweroff`, PID1 receives the event and SIGTERM reaches the runner, but its journal
close blocks until the external grace period expires. The scenario assertions still pass; the new
shutdown assertion correctly rejects this forced cleanup.

A temporary Go `runtime/pprof` goroutine dump in
[invocation `58f790a6`](https://app.buildbuddy.io/invocation/58f790a6-5d13-4592-8360-68a57d0da653)
locates the blocked operation. The journal acquires its mutex and returns its reserved SQL connection;
`sql.DB.Close()` then enters `sqlite3_close_v2`, SQLite's Unix file close, and Linux `close(2)`. Five
seconds later that same syscall is still blocked. This excludes waiting for a journal reader or the
journal mutex in that execution. The temporary probe is absent from the implementation.

KubeVirt v1.8.2 generates its virtiofs daemons as ordinary launcher containers, with no termination
ordering relative to compute. The state filesystem must remain served until the runner closes its
journal and the guest unmounts it. Kubernetes native sidecars provide that lifecycle ordering:
`initContainers` with `restartPolicy: Always` stop after ordinary containers. Conversion must preserve
existing initialization order, daemon resources and security settings. Restrict it to PVC-backed
daemons: KubeVirt's `MissingContainerPathContainers` reconciliation requires ContainerPath daemons
to remain in the ordinary container list.
