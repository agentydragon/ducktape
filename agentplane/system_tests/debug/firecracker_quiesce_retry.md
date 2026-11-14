# Firecracker cleanup waits for a failed seal retry

The fixed-source `6bebe271d8` guest-reuse A/B/B/A passes all four invocations,
four fresh physical actions and six nonempty JUnit cases, without retries or
skips. A creates two separately clean guests; B composes unchanged issuer trust
and live-state assertions on one fresh guest. These are targeted measurements,
not complete required feedback or an isolation-equivalence claim.

| Arm                                                                             | Native invocation | Execution | Guest cleanup    | Imports not skipped |
| ------------------------------------------------------------------------------- | ----------------: | --------: | ---------------- | ------------------: |
| [A1](https://app.buildbuddy.io/invocation/572ec341-d887-49e7-9a5d-27c090a1a7c3) |          343.286s |  300.022s | 20.968s, 76.358s |                   4 |
| [B1](https://app.buildbuddy.io/invocation/21b2e916-d0d0-45ab-b2ba-9e878fc2844c) |          424.462s |  270.136s | 81.449s          |                  12 |
| [B2](https://app.buildbuddy.io/invocation/a9b44bc7-d5af-47b3-8e6d-06df1835b983) |          246.365s |  199.253s | 13.855s          |                  12 |
| [A2](https://app.buildbuddy.io/invocation/289fcb99-c6ef-477e-9d88-034263b14a29) |          357.140s |  313.951s | 69.361s, 67.363s |                   0 |

All arms retain the actual kernel process identity, forward progress, certificate
rotation and real one-minute idle checkpoint/resume. CPU is unknown. Native and
execution comparisons have different queue/import exposure; they cannot establish
a guest-reuse saving by themselves.

Four of six guest cleanups have an initial controller `runner_quiesce` returning
gRPC `Unavailable`, then a successful call about one minute later. Successful
sealing is followed promptly by Pod deletion and observed Sandbox absence. In B2,
the first seal succeeds and cleanup takes 13.855s. The extra minute is the
controller's persisted `quiesceDeadline`, not a minute of VMM deletion.

The failed RPC reaches the Gateway but has no downstream Runtime RPC span. Existing
traces omit error messages intentionally. Runtime thaw or a Controller WatchState
response does not prove that the Gateway's cached destination and elected control
route are ready. The precise failed routing predicate remains unproven.

The branch adds safe routing-phase/result attributes and a verified local-control
acceptance event to the existing Gateway traces. No request bodies, credentials
or free-form errors are recorded. Run the unchanged required two-guest target
with these traces before changing routing, sealing or retry policy. Preserve
same-incarnation fencing, durable Session seals and exact-UID deletion waits.

If a route/cache race is confirmed, prefer repairing that admission boundary;
do not globally shorten the persisted deadline that protects unfinished/stalled
quiescence across watch events and controller restarts. Track follow-up in
[#16099](https://github.com/agentydragon/ducktape/issues/16099).
