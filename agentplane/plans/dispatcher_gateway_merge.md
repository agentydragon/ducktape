# Dispatcher and Gateway as one service

Status: **proposal awaiting
[`DISPATCHER_PERSISTENCE_MODEL_DESIGN`](task_dag.md#dispatcher_persistence_model_design--choose-delivery-ownership-and-service-topology);
not an implementation authorization.** On 2026-11-02 the operator made the Dispatcher database and
queue mandatory, so the proposal's queue-off mode and its rollout steps are gone. Two parts remain
candidates for that decision: one public service and audience in place of `SessionDispatcher` plus the
caller-visible Runtime proxy, and holder-owned delivery. An explicit immediate-or-fail send within the
mandatory queue is a separate open option.

Operator, 2026-10-31 UTC: Session dispatch and Environment dispatch are distinct responsibilities,
because a Session need not live under an environment; they may share a binary or not, and this
merge is left for later. The operator's 2026-10-20 preference for holder-owned delivery was scoped to
the combined-service shape; revisit it with the topology. Existing dispatch ownership and fencing
stay until a reviewed change replaces them. The current queue contract is the
[Dispatcher README](../runner_gateway/dispatcher/README.md); the Gateway's Runtime proxy restriction is
its [authorization contract](../runner_gateway/gateway/SPEC.md).

## Today

One binary and Deployment serve the Dispatcher, the per-Sandbox Runtime proxy and
`EnvironmentService`. The Gateway dials private Runtime mTLS gRPC servers and observes their state
under each Sandbox's `<sandbox>-runner` Lease. The Dispatcher separately uses a `dispatcher-<uid>`
Lease plus its Postgres `sandbox_ownership` fence. Dispatcher callers use the `dispatcher_audience`;
proxy callers use the Gateway's audience.

The Dispatcher delivers by dialing `<sandbox>-control` (`ProxyRunner`, `dispatcher/runner_client.go`),
because the replica owning a Sandbox's queue is often not the replica holding its Runtime connection.
That self-call costs a token check, a network hop and a third caller identity. The ADR row "The
Dispatcher is its own service, not part of the gateway or the controller"
([ADR](../docs/adr_sandbox_session_control.md)) no longer matches the packaging.

## One service

`RunnerGateway` would replace `SessionDispatcher` and the caller-visible Runtime proxy. The private
Runtime mTLS connection stays separate from public caller authentication.

| RPC                                                                                       | Caller                  | Behavior                                                |
| ----------------------------------------------------------------------------------------- | ----------------------- | ------------------------------------------------------- |
| `OpenSession`, `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, `InterruptTurn`     | app, Notification       | the Dispatcher's queue, unchanged                       |
| `GetSessionOpening`, `GetDispatch`, `ListDispatches`, `WatchDispatches`, `CancelDispatch` | app, Notification       | read or change the stored rows                          |
| `ListSessions`, `GetStatus`                                                               | app, Notification       | live read by the replica holding the Runtime connection |
| `Quiesce`                                                                                 | Sandbox Controller only | the same                                                |

- **Messages.** Command and state RPCs keep the Dispatcher's messages (`Dispatch`, `DeliveryState`),
  so the app's stream and Notification's receipts keep working; live reads and `Quiesce` keep the
  Runtime's. "Dispatcher" stays the name of the queue role inside the Gateway.
- **Addresses** follow what a call needs: commands and state go to the stable Service (any replica);
  `ListSessions`, `GetStatus` and `Quiesce` go to `<sandbox>-control` (the holder; ADR D1).
- **One audience, per-RPC authorization.** One audience and one caller list replace
  `dispatcher_audience`/`dispatcher_callers` and the Gateway's. Commands, state and live reads admit
  the app and Notification; waking admits `wake_callers`, a subset of them; `Quiesce` admits only the
  Sandbox Controller. The Gateway's own identity leaves the list once delivery is in process.
- **Proxy.** Delivery would call the held connection in process, so the command methods could leave
  the caller-visible proxy; today's restriction keeps them for Gateway/Dispatcher workload credentials
  across replicas.
- **HTTP facade.** One facade for the one service, routes unchanged.
- **Naming the Sandbox** in a request is unchanged; `DELIVERY_DESTINATION_AUTHORITY_DESIGN` decides it.

## Immediate-or-fail option

If a send may ask not to wait, **immediately** means the Sandbox exists, is `Ready` and the receiving
replica holds its Runtime connection. Anything else is refused with the queue's reason vocabulary: a
missing Sandbox is `NOT_FOUND`; `sandbox_suspended` is `FAILED_PRECONDITION`; `sandbox_not_ready`
(starting, draining, Runtime not connected or held by another replica) is `UNAVAILABLE`. A call lost
mid-flight has an unknown outcome; the caller retries the same `command_id`. Only the holder can
answer immediately, so with several replicas such a request either lands on the holder or forwards to
it, which is the hop and trust ADR D1 declined. Wake-on-send cannot be immediate: a wake needs a
stored command that survives the boot.

## One owner per Sandbox

Holder-owned delivery, **chosen by the operator for the combined shape, 2026-10-20 07:34 UTC:** the
owner of a Sandbox's queue is the replica holding its Runtime connection.

- `<sandbox>-runner` stays. It names the holder, carries the connection epoch (its
  `leaseTransitions`) and the Runtime's facts, and the controller reads it for
  `EnvironmentRuntimeConnected`.
- `dispatcher-<uid>` goes. Its expiry exists to pick one live replica, and the Runtime's stream
  already does: the replica that accepts the connection holds it, and a newer epoch supersedes a
  stale holder.
- The Postgres `sandbox_ownership` fence stays and advances with the connection epoch, so a replica
  that lost the connection cannot settle rows.
- Delivery is a call on the held connection: no `<sandbox>-control` hop and no third caller identity.
  Replicas stop contending for every Sandbox with pending work; each looks only at the Sandboxes it
  holds.
- With no connection (suspended, booting) there is no owner and no delivery; work waits in Postgres.
  Any replica may request the wake; the conditional Sandbox patch on UID and resourceVersion and the
  wake budget keyed by caller, Sandbox UID and target wake generation make concurrent requests one
  resume and one charge. When the Runtime connects, the accepting replica becomes the owner, requeues
  that Sandbox's in-flight rows and delivers.
- A rollout that briefly runs both owners (an old replica by Lease, a new one by connection) can
  deliver a row twice. The Runtime deduplicates on `command_id`, the at-least-once the Dispatcher
  already promises.

Rejected: keeping `dispatcher-<uid>` and delivering in process only when owner and holder coincide.
With N replicas they coincide one time in N, so the self-call and both ownership mechanisms stay.

Rejected: moving the Lease's job into Postgres alone (an `expires_at` heartbeat on the fence row, or a
session-level advisory lock). The fence has no expiry, so it stops stale writers but cannot choose a
live owner; here the connection chooses it, so neither the Lease nor a Postgres liveness mechanism is
needed.

## On approval

Replace the ADR row about the Dispatcher being its own service, update the Gateway and Dispatcher
READMEs and the [system overview](../docs/system_overview.md#system-overview), and stage callers by
the settings rule (stop setting the old audience, then remove the field). A fence-column change is a
Postgres migration landing first and alone.
