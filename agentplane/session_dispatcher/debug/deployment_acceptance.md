# Dispatcher testing deployment acceptance

The deployment from PR #10449 is merged and reconciled in `agentplane-testing`. Initial inspection
found one active replica with `Recreate`, an applied independent CNPG `session_dispatcher` database
owned by `session_dispatcher`, successful migration image `devel-20261014050240-98c04c8`, and
Dispatcher image `devel-20261014065129-c5dc9b4` Ready. A later inspection observed an image rollout
to `devel-20261014071248-5df1aa9`; the acceptance run must record the image it actually exercises.

## Bounded matrix

| Boundary                | Check                                                                                                                                            | Evidence class                               |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------- |
| Caller authentication   | No bearer is refused; a short-lived token bound to a fixture Pod under the allowed testing app account is accepted                               | Deployed probe                               |
| Queue while not Ready   | Create an owned stopped Pod Sandbox, enqueue a setting change, observe `QUEUED_IN_DISPATCHER` with `sandbox_suspended` through `WatchDispatches` | Deployed probe                               |
| Delivery after Ready    | Queue Session opening, explicitly start only that Sandbox, observe opening and command admission plus matching runner journal command ID         | Deployed probe                               |
| Independent persistence | Restart a fixture Dispatcher against its own migrated database and verify queued work survives                                                   | Followup integration check; not yet executed |
| App unavailable         | Exercise startup, authentication, queue and delivery with no app process in the fixture                                                          | Followup integration check; not yet executed |
| Resource cleanup        | Suspend and delete only the recorded Sandbox UID; delete only the recorded caller Pod; confirm PVC removal and quota                             | Deployed probe                               |

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
