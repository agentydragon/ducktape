# Same-principal Session Actions

The `session_delegation` executor forwards the ServiceAccount identity retained with an Action
request to Environment Discovery, Session Dispatcher and History. These APIs enforce their normal
caller-scoped target and Session checks. Actions authenticates with its own projected token for each
destination audience; it carries the original caller's verified namespace, name and UID plus the
durable Action request and execution UUIDs in the shared delegation envelope. Tool arguments never
select the caller. An absent caller UID fails closed.

## Reviewed operations

Action group/action names remain the approved contract names. MCP Edge exposes each configured
direct tool as `<group>__<action>`:

| Action | Direct MCP tool | Current executor |
| --- | --- | --- |
| `environments/list` | `environments__list` | Offered |
| `environments/get` | `environments__get` | Offered |
| `sessions/open` | `sessions__open` | Offered |
| `sessions/submit_input` | `sessions__submit_input` | Offered |
| `sessions/observe_input` | `sessions__observe_input` | Offered |
| `sessions/get` | `sessions__get` | Reserved for the retained snapshot projection |
| `sessions/read_turns` | `sessions__read_turns` | Reserved for the retained turn projection |
| `sessions/cancel_input` | `sessions__cancel_input` | Offered |
| `sessions/interrupt_turn` | `sessions__interrupt_turn` | Offered |

The schemas are defined by the typed Go input records in `contract/inputs.go`; unknown fields are
rejected. Target references include namespace, name and immutable UID. Session and command IDs are
caller-minted UUIDs. An opening accepts one offered runtime configuration and optional setup script;
`additional_instructions` is an additive standing brief and does not start a turn. Submit the first
task separately with `sessions__submit_input`.

`environments__list` returns the owning Discovery page; `environments__get` refreshes one exact
incarnation. Open, submit and cancel results wrap the owning Dispatcher's ProtoJSON receipt; an
interrupt returns its typed ProtoJSON response. Observation returns `session_client.ObserveInput`'s
typed source projections, retaining Dispatcher and History facts, gaps, resume token and sanitized
source errors separately. Protobuf enum strings keep their generated domain names. All successful
values are stored and returned by MCP Edge as structured JSON with the same JSON text fallback.

## Authorization and recovery

The `basic` PolicySet contains an exact-action auto-approval rule for the nine approved operations.
Its callers already have scoped Dispatcher and Discovery reachability, and the receiving services
verify the effective caller's UID and resource authority. A grant to `basic` is the operator-owned
policy assignment; existing PolicyBindings are not rewritten by this adapter. Neither Action
approval nor this delegation path creates or changes caller grants.

Opening and input submission retain caller-held Session/command UUIDs for backend idempotent
lookup. Cancellation first reads the caller-authorized Dispatch receipt, checks that its command,
Session and operation match, then asks Dispatcher to cancel it; a target UID is not needed for this
queued-only lookup. Reads may fail with a sanitized source error. A write error is returned as a
known refusal only for an explicit backend rejection; a transport or deadline error is recorded as
`execution_unknown`. The Action worker does not replay a mutation. Reconcile with the same retained
IDs and inspect effects before starting a new operation.

This adapter only reuses existing environments. It creates no target and adds no wake authority; a
submitted input may still follow the existing backend wake-on-send policy when its authenticated
caller already has that permission. It adds no broker authority path or managed platform-guidance
activation.
