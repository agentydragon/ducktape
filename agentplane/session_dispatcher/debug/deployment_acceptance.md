# Dispatcher testing deployment acceptance

The deployment from PR #10449 is merged and reconciled in `agentplane-testing`. Initial inspection
found one active replica with `Recreate`, an applied independent CNPG `session_dispatcher` database
owned by `session_dispatcher`, successful migration image `devel-20261014050240-98c04c8`, and
Dispatcher image `devel-20261014065129-c5dc9b4` Ready. A later inspection observed an image rollout
to `devel-20261014071248-5df1aa9`; the acceptance run must record the image it actually exercises.

## Bounded matrix

| Boundary                | Check                                                                                                                                            | Evidence class                                |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------- |
| Caller authentication   | No bearer is refused; a short-lived token bound to a fixture Pod under the allowed testing app account is accepted                               | Deployed probe                                |
| Queue while not Ready   | Create an owned stopped Pod Sandbox, enqueue a setting change, observe `QUEUED_IN_DISPATCHER` with `sandbox_suspended` through `WatchDispatches` | Deployed probe                                |
| Delivery after Ready    | Queue Session opening, explicitly start only that Sandbox, observe opening and command admission plus matching runner journal command ID         | Deployed probe                                |
| Independent persistence | Restart a fixture Dispatcher against its own migrated database and verify queued work survives                                                   | Isolated service-lifecycle integration passed |
| App unavailable         | Exercise startup, authentication, queue and delivery with no app process in the fixture                                                          | Isolated service-lifecycle integration passed |
| Resource cleanup        | Suspend and delete only the recorded Sandbox UID; delete only the recorded caller Pod; confirm PVC removal and quota                             | Deployed probe                                |

`//agentplane/acceptance:test_dispatcher_deployed` is a manual controlled-host test. It uses the
testing Service through a localhost port-forward, Kubernetes API lifecycle operations on its own
uniquely named Sandbox, and a fixture-created Pod for Pod-bound authentication. It does not activate
senders, wake on send, use the app HTTP API, restart shared services, create Pods as `claude-ai`, or
write to staging. Caller network-policy ingress is inspected configuration rather than a claim from
the port-forward path; Dispatcher-to-runner delivery exercises the actual deployed backend path.

The setting change opens a Codex Session but asks for no model turn. The existing VM Codex
suspend/resume result can be reused without repeating memory prompts. Multi-replica behavior,
`HARNESS_CONFIRMED`, wake-on-send, and the historical rollout 403 are separate work.

Run after the deployed Dispatcher reports Ready:

```bash
bazelisk test //agentplane/acceptance:test_dispatcher_deployed \
  --test_output=streamed --test_arg=-s --nocache_test_results
```

The test retains partial `dispatcher-deployed.json` evidence with `passed: false` until all service
assertions pass, then adds `cleanup_complete` after its scoped cleanup. A readiness failure or an
authorization refusal is a concrete gate, not a reason for blind reruns.

## First probe: missing Sandbox defaults

The first run at `24c6958c43` failed in [invocation cb5f8526-0074-4ddd-818f-916b7adacf0f](https://app.buildbuddy.io/invocation/cb5f8526-0074-4ddd-818f-916b7adacf0f)
against Dispatcher `5df1aa9`. Pod-bound authentication and the suspended queue passed. Gateway logs
at `2026-10-14T07:30:51.180Z` show `OpenSession` refused with `FAILED_PRECONDITION` because the LLM
proxy model-config lookup returned HTTP 403; the subsequent command was `NOT_FOUND` because the
Session had not been created. `Runner._create` resolves model configuration even without a model
turn. The direct fixture CR had omitted the Sandbox Service's configured default PolicySets,
`basic` and `inference-experiments`. This is probe setup failure, not evidence of a Dispatcher
opening-order race or the unrelated historical rollout 403.

Scoped cleanup removed that run's Sandbox, caller Pod and PVCs. The caller's short-lived token was
bound to the deleted Pod and is revoked. A failure display included bearer text from a helper
argument; the revised probe uses a credential holder with a redacted representation. It also
parses exact deployed defaults, captures failed opening state/reason before cleanup, and uses API
UID deletion preconditions. The caller Pod is an authentication binding object; its readiness is
not a prerequisite for TokenReview, and the probe does not claim its process generated the RPCs.
The corrected bounded run remains pending.

## Second probe: wrong Pod working directory

The run at `6517e55d2f` in [invocation 54bb96be-81cc-4666-ad34-c6671c988d68](https://app.buildbuddy.io/invocation/54bb96be-81cc-4666-ad34-c6671c988d68)
passed caller authentication, the suspended queue and the default PolicySet gate, then its opening
stayed `QUEUED_IN_DISPATCHER` / `delivery_unavailable`. Scoped reads of the opening and command rows
before cleanup and gateway logs show `UNAVAILABLE` with `[Errno 13] Permission denied: '/workspace'`.
The actual Pod template mounts writable state at `/state` and runs UID 1000; successful existing
Pod acceptance uses `/state/work`. The fixture had incorrectly selected `/workspace`. Its command
watch reached its existing deadline; no timeout or retry policy was changed.

The corrected SessionSpec uses the successful Pod helper's working directory and explicit low
reasoning effort. Deadline failures now also retain opening state/reason before cleanup. This
second run's own Sandbox, caller Pod and PVCs were removed by the API UID-fenced teardown. No shared
resources were restarted or forced. This remains a probe setup failure, not deployment acceptance.

## Passed deployed delivery and isolated lifecycle checks

The corrected deployed probe at `bca067f740` passed in [invocation a3f8ead8-434a-4d62-b151-6ba50a81a3fb](https://app.buildbuddy.io/invocation/a3f8ead8-434a-4d62-b151-6ba50a81a3fb)
(103.93 seconds of pytest). Dispatcher and gateway images were `5df1aa9`; controller was `025621f`.
Sandbox UID `d0a0d3a4-fe0c-464e-9e7e-e0bff038a00f` held its command with `sandbox_suspended`, then admitted
opening and command `72ee1d5b-b9e7-4afb-a295-8a8b7e8a6898` once explicitly started. The returned runner
journal admission cursor was 9 and carried that exact command ID and setting payload. The JSON
evidence has `passed: true` and `cleanup_complete: true`. Scoped deletion removed its Sandbox,
caller Pod and PVCs; testing quota returned to 5Gi/80Gi.

`//agentplane/session_dispatcher:test_startup` passed in [invocation 920c7265-ec5e-4eed-a5bd-1d792054c97b](https://app.buildbuddy.io/invocation/920c7265-ec5e-4eed-a5bd-1d792054c97b)
(49.5 seconds). It constructs fresh server, engine, store, watch, authentication resolver and delivery
worker through `serve`; tears them down; starts them again against the same isolated migrated
PostgreSQL clone; verifies the queued row's retained identity/timestamp; and makes the fake Sandbox
Ready. Real gRPC reaches the scripted runner with opening before input, exactly once. No app process
is started. This is service-lifecycle integration, not an operating-system kill or deployed outage.

BuildBuddy mirrored the integration working tree as base `f21ddae950` plus the complete branch and
working-tree patch. The affected test and BUILD blobs match `e31d1a2e17`; source SHA-256 values are
`5c5f1897e7a1df13bebd95c1267c4ea65f12b2ecc4130f29e7c220dec631c779` and
`f02a525a53ad033a5ee1d2afe1ccb82db0a2953c70bc40e12a52a50906f364d8`. The different remote base is not a
claim that the whole invocation ran an unpatched `e31d1a2e17` checkout. No extra live run was used
for this check, and multi-replica behavior remains separate.

## Passed deployed replica failover and deletion broadcast

On 2026-10-14, the `replicas` case passed in
[invocation 46067206-22ee-4274-8215-99fb71fc8a1d](https://app.buildbuddy.io/invocation/46067206-22ee-4274-8215-99fb71fc8a1d):
one test, 181.51 seconds of pytest. Its executed source matches commit `38bba78cd4`;
`test_dispatcher_deployed.py` SHA-256 is
`4c6701919a92cc21cb71ac6e319f7d56a5e576d235b610fcbd89c8329669c71d`.
The deployed server was `devel-20261014194110-bce66fc` and its migration image was
`devel-20261014190949-5a3ee1a`.

The probe queued work for stopped Sandbox UID `9e99d2b7-a5f4-49da-a52c-b3b65e192d9f`, scaled testing
from one Dispatcher replica to two, and deleted the known original owner Pod with UID precondition
`3e718427-ad9c-4433-9a1f-1d572671d270`. A Lease watch observed a new holder and transition count 2.
The queued command retained its creation timestamp across failover. After the fixture Sandbox
started, opening and command `04d16668-cb13-4ac8-bf70-68dad21bda4d` reached runner admission,
with the command recorded at journal cursor 9.

After suspending that Sandbox, another queued command was observed by streams pinned to the two
distinct Dispatcher Pods (`56142383-0c6a-4b5b-92db-a3b18fde56c6` and
`23f250b5-e8c7-48e5-8425-6866b22cc69b`). Deleting the fixture delivered `FAILED / sandbox_deleted`
to both streams. No model turn or app HTTP call was used; authentication used a token bound to an
existing app Pod, not an app-issued credential. This does not claim that the app process was stopped.

The artifact records `passed: true` and `cleanup_complete: true`. Subsequent API reads found no
testing Sandboxes and only the shared `postgres-1` PVC. Testing returned to one Ready Dispatcher
replica; staging remained one Ready replica. #10505 proposes staging activation; its reconciliation
remains a separate rollout gate.
