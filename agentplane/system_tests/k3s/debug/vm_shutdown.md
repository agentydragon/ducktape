# VM shutdown probe, 2026-10-27

The minimal CI guest did not respond to KubeVirt's graceful shutdown request: its initramfs lacked
the `button` and `evdev` modules (`CONFIG_ACPI_BUTTON=m`, `CONFIG_INPUT_EVDEV=m`) and its shell PID1
had no ACPI listener. KubeVirt waited for its grace period, destroyed the domain, then removed the
launcher. The observed cleanup tail was mostly this shutdown path; changing Sandbox finalizer or
live-update sequencing would not have removed it. The baseline probe establishes the failure, not an
end-to-end savings measurement. The guest now loads both modules and runs `acpid` under systemd as
PID1 ([shutdown path](../../deployment/README.md#vm-sandboxes)).

The `test_vm_sandbox_vm` command below is the historical target name used for this measurement;
the current VM suite target is `//agentplane/system_tests/suites:test_vm`.

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
The [CLI retry and invocation identity evidence](../../../../devinfra/buildbuddy/debug/ci_remote_cancellation/vm_shutdown_retry.md)
is consolidated with the remote CI cancellation investigation.

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

## Journal close after ACPI reception

Adding ACPI handling alone exposed a second shutdown dependency. With the correct action installed
at `/etc/acpi/poweroff`, PID1 received the event and SIGTERM reached the runner, but its journal
close blocked until the external grace period expired. The scenario assertions still passed; the
[teardown check for the guest's shutdown-complete marker](../../deployment/README.md#vm-sandboxes)
correctly rejected this forced cleanup.

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
