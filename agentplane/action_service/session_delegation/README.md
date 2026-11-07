# Same-principal Session Actions

Agent workflow: [Session delegation](../docs/agents/session_delegation.md), available with
`agentplane actions guide session_delegation` and MCP `get_guide(name="session_delegation")`.

The `session_delegation` executor forwards the ServiceAccount identity retained with an Action
request to Environment Service, Session Dispatcher and History. These APIs enforce their normal
caller-scoped target and Session checks. Actions authenticates with its own projected token for each
destination audience; it carries the original caller's verified namespace, name and UID plus the
durable Action request and execution UUIDs in the shared delegation envelope. Tool arguments never
select the caller. An absent caller UID fails closed.

## Reviewed operations

Action group/action names remain the approved contract names. MCP Edge exposes each configured
direct tool as `<group>__<action>`:

| Action                    | Direct MCP tool            | Current executor |
| ------------------------- | -------------------------- | ---------------- |
| `environments/list`       | `environments__list`       | Offered          |
| `environments/get`        | `environments__get`        | Offered          |
| `environments/resume`     | `environments__resume`     | Offered          |
| `sessions/create`         | `sessions__create`         | Offered          |
| `sessions/submit_input`   | `sessions__submit_input`   | Offered          |
| `sessions/observe_input`  | `sessions__observe_input`  | Offered          |
| `sessions/get`            | `sessions__get`            | Offered          |
| `sessions/read_turns`     | `sessions__read_turns`     | Offered          |
| `sessions/cancel_input`   | `sessions__cancel_input`   | Offered          |
| `sessions/interrupt_turn` | `sessions__interrupt_turn` | Offered          |

The schemas are defined by the typed Go input records in `contract/inputs.go`; unknown fields are
rejected. Target references include namespace, name and immutable UID. Session and command IDs are
caller-minted UUIDs. Creation accepts one offered runtime configuration and optional setup script;
`additional_instructions` is an additive standing brief and does not start a turn. Submit the first
task separately with `sessions__submit_input`.

`environments__list` returns the owning Discovery page; `environments__get` immediately refreshes
one exact incarnation. It has no wait fields and never resumes a target. `environments__resume` sends
one explicit request using the exact target and opaque token from a fresh get; its result is
acceptance, not readiness. The receiving backend enforces the effective caller's current
same-ServiceAccount policy.
`sessions__get` returns `session_client.GetSession`'s independently sourced retained
Dispatcher Session and History summary. The Session part is the minimal authorized projection; it
omits the creation spec and instructions and does not look up a live environment. Each source keeps
its observed, missing or unavailable state and sanitized error; an interrupted composite read adds
an aggregate `read_error` while preserving facts already read. `sessions__read_turns` returns
`session_client.ReadTurns`' compact retained turns, cursor coverage, gaps, continuation cursor and
sanitized History error. Incomplete turns remain unfinished when a read stops early. These cursors
are continuation hints scoped to a Session and carry no authority.

Create returns the minimal Dispatcher Session in a `session` result envelope; submit and cancel wrap
the owning Dispatcher’s ProtoJSON Dispatch receipt, and interrupt returns its typed ProtoJSON
response. Observation returns `session_client.ObserveInput`'s typed source
projections, retaining Dispatcher and History facts, gaps, resume token and sanitized source errors
separately. Protobuf enum strings keep their generated domain names. All successful values are
stored and returned by MCP Edge as structured JSON with the same JSON text fallback.

## Authorization and recovery

Only policies already bound to the caller can auto-approve these Actions. This adapter does not
change a PolicySet or caller binding. The receiving services verify the effective caller's UID and
resource authority; neither Action approval nor this delegation path creates or changes caller
grants.

Session creation and input submission retain caller-held Session/command UUIDs for backend idempotent
lookup. Cancellation first reads the caller-authorized Dispatch receipt, checks that its command,
Session and operation match, then asks Dispatcher to cancel it; a target UID is not needed for this
queued-only lookup. Reads may fail with a sanitized source error. Resume refusals preserve structured
eligibility or wake-assessment details. An ambiguous resume reply is `execution_unknown`, not a
refusal, and the Action worker never replays it. Refresh the target before making another explicit
resume request; do not replay earlier Session input.

This adapter only reuses existing environments and creates no target or caller grant. Explicit
resume is a reviewed request checked by `EnvironmentService.ResumeEnvironment` against the caller's
existing `resumePolicy`; it adds no caller authority. Submitted input remains governed separately by
the backend's wake-on-send policy.
