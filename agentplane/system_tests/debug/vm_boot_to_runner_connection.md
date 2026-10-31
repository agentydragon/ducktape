# VM boot to Runner connection, 2026-10-31

The [hosted six-VM suite](https://app.buildbuddy.io/invocation/21baa91d-9f05-549e-a08b-6d6d27f45ee8) passed all selected cases. Five `Ready` driver spans lasted 80.7–105 s from Sandbox creation. `WorkloadReady` occurred around 36–41 s, but `RunnerConnected` did not arrive until around 80–105 s. The latter requires a Gateway-observed `WatchState` connection and a Lease renewal matching the current Pod UID and boot ID (`sandbox_controller/controller.go`, `runner_gateway/gateway/runner_observer.go`); the Kubernetes workload condition alone does not establish Runner availability.

The downloaded driver's `test-spans.jsonl` has SHA-256 `7f5c2e27a1e7ad20c678f3def01d26f280ff36956351b7f509fc363f055bdadf`. The hosted invocation tested the final shared-tracing PR source before that PR merged; BuildBuddy's invocation and retained test outputs, rather than a later branch head, are the evidence for this sample.

The test's retained guest-console and central Gateway logs give this sequence. Offsets are from the first timestamped guest kernel console line for each VM; CRI log buffering makes those offsets approximate. The Ready timestamp is the driver's observed condition, not the instant the guest started listening.

| Guest                  | Network target | `/state` mounted | Workspace preparation | Last Gateway dial failure |   Ready | Last failure to Ready |
| ---------------------- | -------------: | ---------------: | --------------------: | ------------------------: | ------: | --------------------: |
| Runner VM `879f4cae`   |        +25.7 s |          +30.1 s |               +41.4 s |                   +45.0 s | +49.5 s |                 4.5 s |
| Runner VM `63800fa1`   |        +25.0 s |          +29.8 s |               +41.2 s |                   +44.6 s | +49.3 s |                 4.7 s |
| OOM VM                 |        +22.4 s |          +26.8 s |               +38.8 s |                   +42.0 s | +47.5 s |                 5.5 s |
| VM 2                   |        +15.3 s |          +19.7 s |               +32.6 s |                   +34.1 s | +39.9 s |                 5.8 s |
| VM 1, supervision case |        +22.5 s |          +26.9 s |               +38.9 s |                   +61.9 s | +66.4 s |                 4.5 s |

The Gateway made 17–26 failed dials per Sandbox. The retained failures are connection refused or no route; they do not show a TLS, identity, or certificate rejection. Once the last failed dial passed, the observed Ready transition took about five seconds. VM 1's extra 23 s is the supervision scenario's intentional work before its listener, so it is not an ordinary boot sample. These five VMs start concurrently; their durations cannot be summed into a suite critical path.

The guest requires the workspace mount and `agentplane-workspace.service` before `agentplane-runner.service` (`agentplane/runner/vm.nix`). In this sample, guest boot, mount, and preparation precede most of the delay after `WorkloadReady`. It would be premature to change Gateway retries, TLS, or the VM image from these data. The guest's `systemd-analyze` was unavailable in a fresh diagnostic run, so the exact systemd critical chain and CPU cost remain unprofiled. KubeVirt's test launcher reported no `/dev/kvm`; enabling KVM without changing the provider environment is not a supported shortcut.

The [fresh diagnostic suite](https://app.buildbuddy.io/invocation/7a4c01e6-c63b-467d-b9b7-1bd6e5e8126f) passed after a guest TCP probe reached the existing telemetry relay at `10.0.2.1:3132`. It retained the launcher relay logs, which show the listener and configured Alloy upstream. That probe was temporary investigation code and is not a change to the test. The current collection patch retains those logs for future runs, so the next boot investigation can distinguish guest reachability from relay startup without changing readiness semantics.
