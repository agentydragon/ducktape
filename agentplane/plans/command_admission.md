# Durable command admission through Sandbox Service

Status: the admission store and channel delivery are merged, and an ambiguous submission settles by
redelivery; the contract they implement is in the
[Sandbox Service API](../sandbox_service/API.md#sessions-and-commands). The [task DAG](task_dag.md)
owns the remaining work.

## Scope

One surface for routing **all supported runner Commands** through Sandbox Service with persistence:
text input, model/reasoning changes, interrupts and session stop. This is not a text-only queue,
a second execution authority, a background dispatcher, or permission to start a stopped harness.
Notification attachments, notification-specific authorization, producer integration and presentation
are separate follow-ups. Do not add an untyped metadata bag
in their place. Retain authenticated caller provenance for the submission itself.

The current app path has a browser recovery outbox, not an app database command queue to retire.
Keep browser recovery for requests that never reached the service. Runner scheduling/admission
remain runner-owned; ordinary command waits must not block interrupts or stop commands.

## Current command path: no app-backend queue to retire

Source inspection at `c38998b5` found a browser outbox, not an app database command queue:

- The composer creates a command ID and `LocalCommands.remember()` persists the full immutable
  command in browser `localStorage` before HTTP or clearing the composer. Unadmitted commands can
  be retried on mount, connectivity recovery or explicit retry, with the same ID and contents.
- The app command endpoint checks archived admission evidence, then relays through Sandbox Service.
  `ThreadContent.admitted_command()` explicitly implements an archive lookup, not an outbox.
- Sandbox Service's `admit_running_command()` attaches to an existing running session and waits for
  the exact runner admission receipt. It does not enqueue offline work or start a stopped harness.
- The runner journals admission before scheduling native work; its durable commands and execution
  scheduling are a separate responsibility that this plan does not replace.

See [browser recovery](../app/frontend/threads/local_commands.ts),
[submission/retry](../app/frontend/threads/thread_commands.tsx),
[app relay](../app/threads/bridge.py), [archive lookup](../app/threads/view/content.py),
[service relay](../sandbox_service/command_relay.py) and [runner journal](../runner/journal.py).
These are source findings, not proof of the deployed version. Do not add an app queue drain or
retirement phase based on the earlier assumption that such a backend queue existed. Keep browser
recovery for requests that never reached the service; reconcile its handoff with service receipts.

## One caller-driven RPC

1. Authenticate the caller and authorize the concrete Session/Sandbox incarnation, including retries.
2. Validate the command envelope and persist it as `pending_admission` before contacting the runner.
3. Immediately forward a snapshot of the runner Command within the same RPC, without service fields.
4. On a matching durable `CommandAdmitted` receipt, record admission and return OK with that receipt.
5. Record a definitive non-admission refusal as rejected and return an error. Timeout, connection
   loss, cancellation and process failure are not proof of rejection: leave admission pending.

`pending_admission` means retained by the service with no definitive admission outcome recorded.
It includes not-yet-sent and possibly-sent commands; no separate unsent/dispatching state is needed.
It promises neither eventual delivery nor safe cancellation. Runner admission is not harness effect.
Caller retries use the same immutable command ID and contents. Conflicts never overwrite a command;
admitted retries return the retained receipt. Review permanent refusal versus retryable pre-admission
unavailability before mapping runner errors to the internal draft's terminal refusal exception.

## Transport-independent core and outbound integration

The coordinator depends on a submit-command operation returning a durable receipt or explicit
refusal, not on `Attach`, replay cursors or channel framing. The public Sandbox Service envelope
is a protobuf/gRPC request containing destination and the runner Command, with a comment reserving
future service-only metadata design space; do not add a parallel Pydantic wrapper or metadata fields
now. The full command is a protobuf message with a oneof, not an operation enum. Preserve its wire
payload and unknown fields/numeric enum values with SQLAlchemy conversion. Validate and snapshot
mutable protobufs before awaiting; unsupported operations still cannot be dispatched.

The runner keeps its journal and reusable admission/receipt lookup separate from transport handlers.
The first adapter reuses today's `Attach`-based relay, including its internal receipt/replay handling;
the public durable submission contract does not expose a replay cursor. This lets admission ship
without inversion. The later runner-initiated [channel](../docs/runner_channel.md) first replaces command delivery;
independent spool Events move afterward. One gRPC connection per runner process multiplexes
Sessions. Postgres notifications are routing/wakeup signals over durable state, not delivery.
Do not first migrate Sandbox Service to new inbound `InsertCommand`/`ListenSpool` RPCs: their logical
operations belong in the outbound protocol. Submission needs no replay cursor; lifecycle controls
must be mapped explicitly rather than accidentally lost when retiring `Attach`.

A submission whose receipt was lost settles only when its command is delivered again with the same
`command_id`, within its open attempt or on a caller's retry: the runner answers a command it already
journaled with its original `CommandAdmitted` entry, even after its harness stopped or its journal was
sealed ([dispatch attempts](../docs/runner_channel.md#dispatch-attempts)). Without a retry it stays
`pending_admission`; the Sandbox Service reads no runner journal to settle it. Positive admission
evidence must not be regressed by a racing refusal. The unique Session/command key and per-command
updates arbitrate retries; do not serialize unrelated submissions or hold DB locks across runner calls.

## Sequencing and acceptance

See the [runner channel](../docs/runner_channel.md) and
[DAG](task_dag.md#2-service-owned-command-admission-and-later-notification-presentation):

1. Review and implement outbound spool replay, committed-prefix acknowledgements and backpressure;
   switch the canary reader without changing archive storage or identity.
2. Move remaining lifecycle/inbound consumers, then expand full outbound support to selected existing
   environments and retire old access. Preserve command IDs, pending outcomes and history on rollback;
   never silently fall back between command routes after an ambiguous send.

Cross-replica dispatch uses `NOTIFY` only to wake readers of durable state. Retained pending status
alone does not authorize future delivery: routing must refer to a bounded active submission/retry
attempt, with its precise deadline/ownership semantics reviewed before channel implementation. Reconnect
or a missed notification must not turn into an offline pending-command drain. Expiry/cancellation
cannot retract a command already sent; persist late admission receipts.

The admission store (`sandbox_service/commands/`) persists submissions in the `sandbox_commands`
database, apart from history. `SubmitCommand` records public-Session commands there. New inbound RPC
prototypes are not a selected rollout prerequisite.

Acceptance covers authenticated destination/retry authorization, immutable/concurrent retries,
unknown wire fields, lost replies settled by redelivery, explicit refusal versus ambiguous disconnect,
racing receipts, reconnect and interrupts while another command awaits admission. Keep status
reads/UI and notification-specific metadata, permissions and producer integration as later outcomes.
