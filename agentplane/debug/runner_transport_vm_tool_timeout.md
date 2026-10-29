# VM sampler tool-result failures

During the runner-transport PR's integration with the sampler-Pod fixture, the forward VM
case timed out at the second `Endpoint.next()`, after setup and the first model response.
Both Pod modes and the reverse VM case passed.

- Failed execution: [bdc405a1](https://app.buildbuddy.io/invocation/bdc405a1-002e-49a8-9046-3503186733af).
  Hosted CI [1c07a35c](https://app.buildbuddy.io/invocation/1c07a35c-0c75-5909-9286-0a063089d005)
  returned the same VMI, Pod and session artifact IDs; these are one shared execution,
  not independent reproductions.
- The failing forward Pod logged `central sampler execution stopped` with `EOF` at
  00:32:31.991 UTC, before VM shutdown began at 00:32:44.299. The log does not identify
  which connection produced the EOF, and the failure occurred outside the existing
  session-diagnostics capture boundary.
- Fresh diagnostic run [4c09d43d](https://app.buildbuddy.io/invocation/4c09d43d-b17f-4af3-8c5d-adbdba6c597c)
  passed both VM cases, with different workload IDs and zero failures or skips.
  No runtime behavior, assertions or timeouts changed.

`test_central_sampler.py` now captures sampler state, retained History, Sandbox/Lease state
and bounded control-service logs if the tool-result model request fails. The original
failure remains unexplained; the passing rerun does not establish a runtime fix.

## Reverse VM failure with tool work in progress

The later [7b4811b6 invocation](https://app.buildbuddy.io/invocation/7b4811b6-6465-4cd1-b080-de4fdf4696c7)
at `7786598a2b24ca690c756f166777650805351565` passed the forward VM case but failed
the reverse case at the second `Endpoint.next()`. Its retained History makes the 10-second
provider wait a downstream symptom: setup finished, the first model response produced a tool
call, and the VM executor started process `92e5cda1-982c-47ae-8b1f-0de98b0a9969`. The first
`process/read` returned `VM_PID1 systemd` with `exited: false` at 01:11:57.903 UTC. A second
`process/read` was dispatched at 01:11:57.908; at 01:11:57.919 the sampler recorded
`command_failed`, a blocked Session, and an unresolved cell intent. No tool result or second
model request was retained. The Sandbox still reported `RunnerConnected=True`; its Lease's
last renewal was at 01:11:57.918. These states do not establish that the second read returned.

The exact sampler execution error is missing. The failed Session's sampler Pod was UID
`d223d923-e645-434b-8b39-4bfd97492cdb`, started at 01:11:45 with zero restarts. The
published sampler Pod state and empty logs instead belong to a replacement Pod started at
01:14:30 for the passing forward case. Fixture cleanup captured only the last Pod, overwriting
the earlier case's names. VM console logs show control-stream connection errors before the
sampler scenario, then a routing error during test teardown; neither locates a stream failure
at the second read. The available artifacts contain no reply to that read or runner journal
entry for it, so the underlying stop remains unresolved.

The fixture now captures sampler Pod state, container logs, and its EmptyDir session logs under
`test.outputs/central-sampler-pods/<pod-uid>/` both at failure time and at fixture cleanup.
Session diagnostics use the Session ID in their filenames so later cases cannot replace them.
This is diagnostic retention only; it does not change the timeout, assertions, or blocked-state
behavior.

Two fresh noncached runs with the diagnostic retention change
([2240c2ff](https://app.buildbuddy.io/invocation/2240c2ff-0814-49b1-8e08-76e60fe18fa6))
passed both VM modes: four passing cases across separate actions. Their artifacts retain
separate state and logs for all four sampler Pods. No runtime behavior, assertions or timeouts
changed; these passes do not explain the earlier execution failure.
