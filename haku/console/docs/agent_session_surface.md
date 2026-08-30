# Agent-facing session surface (haku-console)

Design note for the multi-agent read/drive surface. Supersedes the v0 "worker" tool
naming (`dispatch_worker` / `get_worker_result` / `get_worker_provisioning`).

> **Status: first cut (v0).** A starting interface, not a settled one — expect a better one
> as we use it. The part most likely to be reworked is the **authorization model for who may
> send messages into a conversation, and as which role**; the config-declared profile policy
> below is a deliberate v0 stand-in for a richer participant/capability model (see Deferred).

## Principle

Expose the **real entities** — sessions and their sandboxes — not a "worker" fiction.
"Worker" is a _role_ a session plays under dispatch, never a typed thing with its own
status. Every agent-facing tool is a **thin wrapper over the same `SessionService`
methods the SPA/API already drive** (path convergence): no parallel implementation, and
the agent sees what the operator sees.

## Entities

- **Conversation** — identity; runs one or more sessions.
- **Session** — a runtime attempt on a harness, owns a sandbox/container; has a lifecycle
  status and turns.
- **Sandbox/container** — the provisioning lifecycle behind a session (claim → pod → runner).

## Reads (all derived, pass-through, scoped)

- **`session_status`** _(new; merges provisioning + activity)_ — the session's full derived
  live status: **sandbox provisioning** (the existing `SessionProvisioningView` — claim / pod /
  runner: queued / pulling / quota-hit / ready / …, reusing `SessionService.sandbox_provisioning`)
  **and activity** (`idle | streaming | running_tool | awaiting_input | ended`, from the latest
  turn's state + session lifecycle). One read answers "where is this session, and what is it
  doing." Derive, don't store. Subsumes today's `get_worker_provisioning`.
- **`session_outcome`** _(+ turns)_ — real session status + latest-turn outcome, replacing
  `get_worker_result`'s `running/done/failed` coarsening.
- **`list_sessions`** — enumerate the sessions in scope (replaces the `list_workers` idea).

## Actions

- **`dispatch`** _(exists)_ — open a session on a harness for an Agent + seed the opening
  prompt. Reuses `create_conversation` + `enqueue_conversation_prompt`. **Operator-approved
  per call.**
- **`send_message`** _(new, v0)_ — send a message into a session, landing in the **user
  role**. Thin wrapper over `enqueue_conversation_prompt` (the SPA's own path), addressed by
  `session_id` → its conversation. The steer/drive primitive.

## v0 scoping — config-declared

A config-declared policy: access profile **`haku` may read + send-into
`public-coder-agent`-level conversations**. `haku → public-coder` **send is
auto-approved**; **dispatch stays operator-approved**. Reuses the existing profile-DAG read
scope (`can_read_profiles`) for reads; adds the send-into half at profile granularity. This
**sidesteps per-conversation ownership** for v0 — there is no "this session was launched by
this agent" record, and none is needed while `haku` is the sole orchestrator of the
public-coder profile.

## Deferred (explicitly not v0)

A **per-conversation capability**: a `session ↔ conversation` attachment relation
(kinds: _assistant-seat_ vs _unwrapped-user-send_), or a multi-participant "rollout" entity,
that would make "my sessions" exact and 1-N sending clean — possibly via the grant model.
The config-declared profile policy is the v0 stand-in. Cross-agent visibility then becomes
"grant the read/send capability to other principals," with no new mechanism. Overengineering
for v0.

**This is the interface most likely to change.** The real question the v0 policy only
approximates is **who may send messages into a conversation, and as whom** — an agent driving
the user seat, one agent relaying as another, a human vs. an agent author, wrapped
(`session <id> says: …`) vs. unwrapped. Treat the v0 `send_message` + profile policy as a
placeholder for that model, not its final shape.

## Mapping from the v0 "worker" tools

| v0 tool                   | becomes                                                |
| ------------------------- | ------------------------------------------------------ |
| `get_worker_provisioning` | folded into `session_status` (provisioning + activity) |
| `get_worker_result`       | `session_outcome` (+ turns) read — real states         |
| `dispatch_worker`         | `dispatch` action                                      |
| —                         | + `send_message`, `session_status`, `list_sessions`    |

"worker" survives only as prose for the dispatch role.

## Placement

Fold the session reads beside the existing conversation reads (`haku_conversations` already
hosts `get_worker_result`, `read_session_frames`, `read_conversation_items`); keep `dispatch`

- `send_message` as the actions. The `workers` / `dispatch_v0` server-name question mostly
  dissolves.

## Implementation notes

- **`send_message`**: wrap `SessionService.enqueue_conversation_prompt(operator_id,
conversation_id, text, origin=user)`; resolve `session_id → conversation`. Same path the
  SPA uses to add a user turn.
- **send auto-approval**: a new auto-approval check keyed on
  `(caller agent/profile, target session's access_profile)` — auto-approve
  `haku → public-coder`. This differs from the existing per-profile _tool allowlist_ because
  it inspects the **target session's** profile, so it is a small, distinct policy addition.
- **`session_status` derivation**: compose `SessionService.sandbox_provisioning`
  (`SessionProvisioningView`) with the latest turn's state (in-flight / running-tool /
  answered) + session lifecycle; one pure read, no stored status column.
- All reads pass-through, fenced by the caller's scope (profile-DAG in v0).

## Build order (independently landable PRs)

1. `session_status` — one read composing sandbox provisioning (`SessionProvisioningView`,
   subsuming `get_worker_provisioning`) + activity.
2. `session_outcome` — real session/turn states, replacing `get_worker_result`.
3. `list_sessions`.
4. `send_message` + the config-declared read/send policy + its auto-approval.

Each is one self-contained change; dispatch in parallel, land as they pass review.
