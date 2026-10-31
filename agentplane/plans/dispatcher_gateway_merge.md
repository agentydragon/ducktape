# Dispatcher and Gateway as one service

Status: **proposed; ownership chosen, the rest not reviewed.** The operator asked for this write-up (2026-10-20
07:14 UTC) and chose holder-owned delivery (07:34 UTC). Nothing here starts before they approve the whole design. It answers the open decision `DISPATCHER_PERSISTENCE_MODEL_DESIGN` in the
[task DAG](task_dag.md). Read at `devel` dd032c46. The plan burns down: a step leaves when it lands, and the file
goes with the last one, after its durable content moves to the Gateway and Dispatcher READMEs and the
[session-control ADR](../docs/adr_sandbox_session_control.md).

## Proposal

Operator, 2026-10-20 07:13 UTC:

> dispatcher basically started like a queue on top of gateway. I think we should merge the grpc services into
> one, and make it operate in one of two configured modes - queue on (in postgres) or off. Off means that if
> your message/command cannot immediately go to the runner, you get an error. And I guess wake-on-message as
> a feature would require the queue.

This design takes that as the shape: keep the queue, and add the direct mode as a setting of the same service. The
direct mode is not a second contract. It is the same service with the queue off, refusing where the queue would
wait.

## Today

One binary and Deployment serve the Dispatcher, per-Sandbox Runner proxy and executor gateway.
The Gateway dials private runner mTLS gRPC servers and observes `Runner.WatchState` under each
Sandbox's `<sandbox>-runner` Lease. The Dispatcher separately uses `dispatcher-<uid>` plus its
Postgres `sandbox_ownership` fence. Dispatcher callers use `session-dispatcher`; proxy callers use
`agentplane-runner-gateway`. The runner connection uses certificate identities rather than those
public caller tokens.

The Dispatcher delivers by dialing `<sandbox>-control` with the Gateway's own token (`ProxyRunner`,
`dispatcher/runner_client.go:19`, wired at `main.go:237`), because the replica owning a Sandbox's queue is
often not the replica holding its runner. That self-call costs a token check, a network hop and a third caller
account. The ADR row "The Dispatcher is its own service, not part of the gateway or the controller" (2026-10-14,
`docs/adr_sandbox_session_control.md:608`) no longer matches the code.

## One service

`RunnerGateway`, in package `agentplane.runner_gateway`, replaces `SessionDispatcher` and the caller-visible
`Runner` proxy. The private runner mTLS connection stays separate from public caller authentication.

| RPC                                                                                       | Caller                  | Queue on                                                                     | Queue off                                |
| ----------------------------------------------------------------------------------------- | ----------------------- | ---------------------------------------------------------------------------- | ---------------------------------------- |
| `OpenSession`                                                                             | app                     | stored; delivered once the Sandbox is `Ready`, before the Session's commands | the runner's `OpenSession` now           |
| `SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`                                     | app, Notification       | stored, in order per Session; may wake                                       | delivered now                            |
| `InterruptTurn`                                                                           | app                     | not queued: delivered at once to a `Ready` Sandbox, else `NO_OP`             | the same                                 |
| `GetSessionOpening`, `GetDispatch`, `ListDispatches`, `WatchDispatches`, `CancelDispatch` | app, Notification       | read or change the stored rows                                               | `FAILED_PRECONDITION`: nothing is stored |
| `ListSessions`, `GetStatus`                                                               | app, Notification       | live read by the replica holding the runner                                  | the same                                 |
| `Quiesce`                                                                                 | Sandbox Controller only | the same                                                                     | the same                                 |

- **Messages.** Command and state RPCs keep the Dispatcher's messages, `Dispatch` and `DeliveryState` with their
  decided names, so the app's stream and Notification's receipts keep working. The live reads and `Quiesce` keep
  the runner's messages. The Dispatcher stays the name of the queue role inside the Gateway.
- **Addresses.** As today, by what a call needs and not by mode: commands and state go to the stable Service (any
  replica); `ListSessions`, `GetStatus` and `Quiesce` go to `<sandbox>-control` (the holder; ADR D1, unchanged).
- **One audience, per-RPC authorization.** `agentplane-runner-gateway` replaces `session-dispatcher`, and one
  caller list replaces `dispatcher_callers` and `gateway_callers`. Commands, state and live reads admit the app and
  Notification. Waking admits `wake_callers`, which must be a subset of them. `Quiesce` admits only the Sandbox
  Controller, as `gateway/proxy.go:91` does. The Gateway's own account leaves the list once delivery is in
  process. The app and Notification already hold a token for this audience, so no new grant is needed first.
- **Proxy.** The command methods leave the caller-visible proxy, which is what
  `PROXY_START_METHODS_DISPATCHER_ONLY` asks for; delivery calls the held connection in process.
- **HTTP facade.** One facade for the one service, routes unchanged.
- **Naming the Sandbox** in a request is unchanged; `DELIVERY_DESTINATION_AUTHORITY_DESIGN` still decides it, and
  this service carries whatever it decides.

## Two modes

The queue is on exactly when its database URL is set. There is no separate flag, and the other queue settings (the
per-Session cap, `wake_callers`) are rejected at startup when set without the URL, so a deployment cannot say
"off" and also configure a wake.

|                     | Queue on                                                                       | Queue off                                                                                |
| ------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| Accepting a command | stores it in Postgres, then answers                                            | delivers it to the connected runner, then answers                                        |
| Runner unavailable  | waits as `QUEUED_IN_DISPATCHER`, with a `reason`                               | refused at once, with the same `reason`                                                  |
| Wake-on-send        | yes, with `spec.wakeOnSend` and a wake caller                                  | no, whatever `spec.wakeOnSend` says                                                      |
| Database            | `session_dispatcher`, the queue migrator init container, a `LISTEN` connection | none                                                                                     |
| Replicas            | any number                                                                     | one in practice: a command landing on a replica that does not hold the runner is refused |

**Immediately** means the Sandbox exists, is `Ready` (the controller's condition) and the receiving replica
holds its runner connection. Anything else is refused with the queue's own reason vocabulary: a missing Sandbox is
`NOT_FOUND`; `sandbox_suspended` is `FAILED_PRECONDITION`; `sandbox_not_ready` (starting, draining, runner not
connected or held by another replica) is `UNAVAILABLE`. The runner's own statuses pass through as the proxy passes
them today. A call lost mid-flight is `UNAVAILABLE` or `DEADLINE_EXCEEDED` with the outcome unknown; the caller
retries the same `command_id`, which the runner deduplicates while its journal is retained.

**Wake-on-send needs the queue** for two reasons. A suspended Sandbox has no runner, so nothing can be delivered
immediately. And a wake takes an image pull and a boot, so the sender must be able to leave and the command must
survive a restart in between, which only a stored command does. With the queue off a send to a suspended
Sandbox answers `sandbox_suspended`, and `spec.wakeOnSend` stays a Sandbox property that does nothing.

With the queue on, the complete destination (Sandbox and Session), the `command_id` and the payload are stored
before the answer and before any wake request, and that receipt is not a runner admission. Runner-local
persistence is no substitute for the queue: it protects work only after the runner has committed its admission,
and it accepts nothing while the runner is absent.

**Queue off runs one replica** because the alternatives were already rejected: a replica forwarding to the holder
adds the hop and trust that ADR D1 declined, and dialing `<sandbox>-control` for commands makes callers depend on
the mode.

## Command states

| State                     | Queue on                                                                                                                                                                                                    | Queue off                                                              |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `QUEUED_IN_DISPATCHER`    | stored, not sent; `reason` says what it waits for (`sandbox_suspended`, `sandbox_resuming`, `sandbox_not_ready`, `wake_on_send_disabled`, `wake_rate_limited`, `runner_refused_work`, `runner_unreachable`) | never returned                                                         |
| `DELIVERING_TO_RUNNER`    | a call to the runner is in flight; if the owner dies the next owner requeues it under the same `command_id`                                                                                                 | never returned: the call in flight is the RPC the caller is waiting on |
| `RUNNER_ADMITTED`         | the runner journaled it (`admission`)                                                                                                                                                                       | the answer to the call, with `admission`                               |
| `HARNESS_CONFIRMED`       | no writer today                                                                                                                                                                                             | the same                                                               |
| `NO_OP`                   | `InterruptTurn` on a Sandbox that is not `Ready`                                                                                                                                                            | the same                                                               |
| `FAILED`                  | terminal: a runner status retry cannot fix, a wake not completed in ten minutes, a Sandbox deleted while queued                                                                                             | never stored; the same causes are errors with the runner's own code    |
| `CANCELLED_IN_DISPATCHER` | `CancelDispatch` while queued                                                                                                                                                                               | never returned; nothing is stored to cancel                            |
| `UNKNOWN`                 | the Sandbox was deleted while delivering or admitted                                                                                                                                                        | never returned                                                         |

In both modes the durable record of what the runner admitted is its journal and the History Service, not the
Gateway.

| Concern             | Queue on                                                                                                     | Queue off                                                                                    |
| ------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Per-Session order   | first in, first out; an opening before its commands                                                          | none added: calls forward as they arrive, so a sender that needs order awaits each admission |
| Cancellation        | `CancelDispatch` until delivery begins; after that the outcome is the runner's                               | nothing to cancel; a cancelled RPC after the call left has an unknown outcome                |
| Duplicate opens     | `session_id` is the key: the same Sandbox and request return the stored opening, another is `ALREADY_EXISTS` | the runner's `OpenSession` is idempotent on `session_id` by the same rule                    |
| Sender fails        | the stored command still delivers                                                                            | the sender retries the same `command_id`                                                     |
| Replica fails       | the next holder requeues that Sandbox's in-flight rows and redelivers                                        | in-flight calls fail `UNAVAILABLE`; the outcome is unknown, retry                            |
| Runner storage lost | terminal for runner-owned state; nothing is rebuilt from the queue or History                                | the same                                                                                     |

## One owner per Sandbox

Queue off needs no owner: nothing is stored, and only the replica holding the runner can act.

Queue on, **chosen by the operator, 2026-10-20 07:34 UTC:** the owner of a Sandbox's queue is the replica holding
its runner connection.

- `<sandbox>-runner` stays. It names the holder, carries the connection epoch (its `leaseTransitions`,
  `gateway/records.go:134`) and the runner's facts, and the controller reads it for `RunnerConnected`.
- `dispatcher-<uid>` goes. Its 30 s expiry exists to pick one live replica, and the runner's stream already does:
  the replica that accepts the connection holds it, and a newer epoch supersedes a stale holder.
- The Postgres `sandbox_ownership` fence stays and advances with the connection epoch, so a replica that lost the
  connection cannot settle rows.
- Delivery is a call on the held connection. No `<sandbox>-control` hop, no Gateway token for the Gateway itself,
  and no third caller account. The other replicas stop contending for every Sandbox with pending work every ten
  seconds; each looks only at the Sandboxes it holds.
- With no connection (suspended, booting) there is no owner and no delivery; work waits in Postgres. Any replica
  may request the wake, and concurrent requests make one resume and one charge: the patch is conditional on the
  Sandbox's UID and resourceVersion (`dispatcher/sandbox_kubernetes.go:177`), the budget row is keyed by caller,
  Sandbox UID and target wake generation (`dispatcher/wake.go:10`), and the wake time is recorded once. When the
  runner connects, the accepting replica becomes the owner, requeues that Sandbox's in-flight rows and delivers.
- A rollout that briefly runs both owners (an old replica by Lease, a new one by connection) can deliver a row
  twice. The runner deduplicates on `command_id`, the same at-least-once the Dispatcher already promises.

Rejected: keeping `dispatcher-<uid>` and delivering in process only when owner and holder coincide. With N replicas
they coincide one time in N, so the self-call stays and two ownership mechanisms stay with it.

Rejected: moving the Lease's job into Postgres alone (an `expires_at` heartbeat on the fence row, or a
session-level advisory lock). The fence has no expiry, so it stops stale writers but cannot choose a live owner;
here the connection chooses the live owner, so neither the Lease nor a Postgres liveness mechanism is needed.

## What queue off removes

It removes the Gateway's database, not CNPG. The shared cluster's logical databases are `app`, `sandbox_service`
(History), `actions`, `egress`, `mcp_edge`, `notifications`, `session_dispatcher` and, where enabled,
`central_sampler` (`cluster/cdk8s/agentplane/database.py:34`), and Indexing has its own. A deployment is
database-free only if it also omits every other consumer, as the tier in `CNPG_FREE_K3S_INTEGRATION_TIER` does for
Notifications, History and Electric. The Gateway sheds the `session_dispatcher` database and Secret, the Goose queue migrator init
container and its Flux health-check gate, the `LISTEN` connection, the delivery worker and the wake budget. That
node's edge from `DISPATCHER_PERSISTENCE_MODEL_DESIGN` stays; its other blockers are unchanged.

## What callers see

- **App.** One client, one audience. With the queue on nothing changes. With it off, a send to a Sandbox that is
  not `Ready` fails at once with the reason, and the thread shows "suspended" or "runner not connected" in place of
  a queued state. The app follows the state stream only for a command whose response is `QUEUED_IN_DISPATCHER` or
  `DELIVERING_TO_RUNNER`, so it needs no mode setting.
- **Notification.** It stores the notice and its command ID before calling. Queue on: as today, a queued answer
  means the Gateway owns the retry. Queue off: a refusal is an error that Notification already treats as a failure of
  something it calls, which a later claim retries (`worker/destination.go:37`). A notice for a suspended Sandbox
  waits until the Sandbox runs, with no wake.
- **`agentplane` CLI.** It has no Gateway command today. A command for sending to a Session would use the facade of
  this service and see the same states, with a queue-off refusal as the facade's `google.rpc.Status` JSON.

## Order of work

Each step is its own PR or batch, and each that changes traffic updates the README system overview. Steps 4 and 5
are independent once step 3 has landed.

1. **Serve `RunnerGateway` beside the old services.** Gateway image only. It wraps the existing Dispatcher and
   proxy implementations, so behavior is identical, and the facade switches to its routes, which equal the old
   ones. Nothing changes for callers until the image is pinned.
2. **Callers move**, after step 1 is pinned in both environments: the app, Notification, the acceptance suite and
   the K3s fixture, one PR per caller. Their `session_dispatcher` settings go by the settings rule: stop setting,
   then remove the field.
3. **Remove the old surface**, after every caller's image is pinned: `SessionDispatcher` and its generated stubs,
   the command methods on the caller-visible proxy, `dispatcher_audience`, `dispatcher_callers` and the
   `session-dispatcher` Service. Manifests drop them first, the fields after the rollout. The Gateway's own
   account stays in the caller list until step 4.
4. **Holder-owned delivery.** A Postgres migration first and alone if the fence's columns change, then the code, as
   in the Gateway README's schema steps. Afterwards the `dispatcher-<uid>` Lease RBAC rule and the Gateway's own
   caller account are removed, once no replica of the old version runs.
5. **Queue off.** Image first: the database URL becomes optional and the immediate path lands. Then a manifest
   renders a Gateway without the queue database, Secret and migrator Job, and the K3s fixture drops the Gateway's
   PostgreSQL.

On approval, the ADR row about the Dispatcher being its own service is replaced, the Gateway and Dispatcher
READMEs describe the modes, and `PROXY_START_METHODS_DISPATCHER_ONLY` leaves the DAG with the step that finishes it.
