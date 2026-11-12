# Repeated gateway-crash Lease-owner assertion failures

The MCP gateway-crash test failed twice at the same Lease assertion after killing the
`runner-gateway` process and scaling its Deployment to zero. In both runs, the Lease kept the
same metadata UID, but its `spec.holderIdentity` field was absent when the test fetched it after
the Pod deletion wait. The test therefore raised `KeyError: 'holderIdentity'` at
[`central_sampler.py`](../scenarios/central_sampler.py).

| BuildBuddy invocation                                                                                             | Source                                                     | Observation                                                                                                |
| ----------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| [5e130ff2-eac8-5103-853b-0b72f95dbee7](https://app.buildbuddy.io/invocation/5e130ff2-eac8-5103-853b-0b72f95dbee7) | PR #15731, head `33ee756df59963cc793857edfc32b7032977982b` | `test_gateway_crash_settles_live_central_read_and_accepts_next_input` fails at the Lease-holder assertion. |
| [d51e2ec6-fff0-5dea-8e16-35e75682f5f9](https://app.buildbuddy.io/invocation/d51e2ec6-fff0-5dea-8e16-35e75682f5f9) | Earlier target-history failure, commit `dbe9fba9`          | Same test, same missing field, same assertion. Neighboring target-history runs passed.                     |

The authorized failed-job rerun of [workflow 43144386465, attempt 2](https://github.com/agentydragon/ducktape/actions/runs/43144386465/attempts/2)
passed on the same PR head; its [BuildBuddy invocation](https://app.buildbuddy.io/invocation/57a178b5-bd62-5f21-a9c9-f3f3fd859e09)
reports this target passed in four minutes. The failure did not reproduce on that retry, but this
does not establish its cause.

The assertion checks that an abrupt process kill did not turn into a graceful Lease release:
after the kill and scale-down, the Lease must retain both the old holder and its transition
count. Those checks remain unchanged.

## Established mechanism

The original failure's `containerd-retained.log` and `process-spans.jsonl` establish this sequence
on 2026-11-12 UTC. Retrieve them with `bbapi artifact download <invocation> <target>/<artifact>`;
the target is `test_gateway_crash` and artifact paths begin with `test.outputs/`.

| Time UTC       | Evidence                                                                                                           |
| -------------- | ------------------------------------------------------------------------------------------------------------------ |
| `03:00:09.876` | Original Gateway container `5e50a550…`, host PID `5161`, exits with status `137`.                                  |
| `03:00:10.115` | Kubelet starts container attempt `1`, ID `9e431d89…`, in the same Pod sandbox `85c72471…`.                         |
| `03:00:10.481` | The restarted Gateway opens `EnvironmentRuntime/WatchState`; its span records observed and published Runner facts. |
| `03:00:11.007` | Deployment scale-down calls `StopContainer` on `9e431d89…` and sends SIGTERM.                                      |
| `03:00:11.014` | The restarted Gateway's `WatchState` ends.                                                                         |
| `03:00:11.046` | Replacement process, host PID `9903`, exits successfully.                                                          |

The Pod is `runner-gateway-b49bfcd6b-zcvgq`, UID `1217573a-b8e4-483d-bf90-053ed504c9d3`.
The restart races the fixture's sequential SIGKILL and Deployment scale-down commands.

The historical `d51e2ec6-fff0-5dea-8e16-35e75682f5f9` failure has the same mechanism on
2026-11-11 UTC: original container `a7fdb80c…` exits `137` at `06:22:51.488`; container attempt `1`,
`57f9f518…`, starts at `06:22:51.770`; its `WatchState` opens at `06:22:52.092` and publishes facts;
scale-down sends it SIGTERM at `06:22:52.662`. Match daemon records to the invocation's test window
(`06:20:10`–`06:24:02`), since retained node logs can include earlier tests.

[`observeRunner`](../../runner_gateway/gateway/runner_observer.go) claims a new Lease epoch
before opening `WatchState`. Its deferred cleanup closes the direct Runner connection and calls
[`connectionRecords.release`](../../runner_gateway/gateway/records.go), which clears the holder
and renewal only when Lease UID, holder and epoch still match. This is the sole connection-Lease
holder-clearing writer. The restarted process runs that cleanup on SIGTERM; the process killed
with SIGKILL cannot. Reusing the Pod name lets the restarted process reclaim immediately, despite
the previous process's unexpired Lease. The assertion observes a graceful release of that new claim.

## Candidate repair and validation

The abrupt fixture sends SIGSTOP to the verified container and confirms the host process is
stopped. It then scales the Deployment to zero, waits for that Pod's deletion timestamp and
kubelet's pending SIGTERM on the stopped process, and sends SIGKILL to the same captured container
ID. The pending signal proves kubelet observed termination; the API timestamp alone cannot.
The process-status file stays open across the wait so PID reuse cannot substitute another task. The stopped process cannot run a graceful signal handler,
and Pod termination precedes its death. A `finally` kills the stopped process even if scaling,
waiting or diagnostics fail. Ordinary graceful-roll scenarios keep their existing sequence.

The fixture writes `gateway-crash-{before,process,terminating,deleted}.json` as undeclared outputs.
They retain the Pod/container identity, PID, restart count, stopped process status and Lease phases.
The original Lease UID, holder, transition count and no-ready-endpoint assertions remain intact.

Focused RBE controls are running:

- Original ordering, deliberately waiting for the restarted process to claim a new epoch before
  scale-down: [28f0f32f-62f4-4055-be66-ea3f60d885b7](https://app.buildbuddy.io/invocation/28f0f32f-62f4-4055-be66-ea3f60d885b7).
- Repaired ordering, deliberately keeping the original process stopped until kubelet has queued
  SIGTERM: [17352c08-b9bb-4bd6-9676-32be63a8b4bb](https://app.buildbuddy.io/invocation/17352c08-b9bb-4bd6-9676-32be63a8b4bb).

Repository hooks pass. These controls are pending; they do not yet establish repaired behavior.
