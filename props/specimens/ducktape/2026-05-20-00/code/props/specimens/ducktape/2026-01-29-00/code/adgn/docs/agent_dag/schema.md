# Canonical Agent DAG Schema (Backend‑Agnostic)

This file defines the logical, storage‑agnostic schema for representing an agent’s state and history in a graph. It captures types, events, nodes/edges, and invariants derived from the current adgn repo (approval policy runtime, runtime/exec, MCP, OpenAI Responses API). Any backend (Git+JSONL, property graph, IPLD, SQL/Dolt, TerminusDB) should project to/from these shapes without loss.

## Conventions

- IDs
  - `AgentId`: string (stable identifier for the agent)
  - `RunId`: string (UUID recommended)
  - `EventId`: string (sha256 over canonical JSON of the event payload)
  - `NodeId`: string (per‑type unique id; often same as `EventId` for event‑backed nodes)
- Time
  - `ts`: ISO‑8601 UTC timestamp with Z
  - Bitemporal (optional): `valid_from` and `valid_to` timestamps for nodes/edges that model evolving truth
- Links/Refs (typed)
  - `EventRef`: `{event_id: str}`
  - `UriRef`: `{uri: str}` (e.g., `resource://approval-policy/policy.py`)
  - `PathRef`: `{path: str}` (filesystem path within a volume or repo)
  - `ExtRef`: `{kind: str, id: str}` (e.g., external object id, stream id)

## Design principles and pattern

- Functional core, imperative shell
  - Core: immutable facts (events, snapshots, heads) in a DAG for time‑travel and forks.
  - Shell: effect handlers/controllers that attempt to realize intents in the real world; they may fail, retry, or vanish.
- Spec vs status (controller/operator pattern)
  - Spec: intents/desired state (e.g., plan to run a tool, fork with a resource plan).
  - Status: observed outcomes (tool_result, liveness, snapshot_created). Reconciliation loops converge status toward spec.
- Event taxonomy
  - Intents (spec), Observations (status), Transitions (interruptions/substitutions), Provenance (derived‑from/summarizes/restores‑to).
- Idempotency and correlation
  - Stable ids for commands/events; idempotent handlers; correlate request→response chains.
- Sagas/compensations
  - On failure, record compensations (e.g., unpin on failed publish). Prefer level‑based reconciliation over edge‑triggers.
- Liveness and leases
  - Treat liveness as advisory; only events/snapshots are durable. Leases expire and must be re‑acquired.
- Fork/time‑travel policy (per resource)
  - keep | snapshot | discard, chosen by capabilities/cost. Non‑restorable/imperative resources can be kept by a single branch, snapshotted (if supported), or discarded.

## Enumerations

- EventKind
  - `model_request`, `model_response`, `history_append`
  - `tool_call`, `tool_result`
  - `mcp.resource_updated`, `mcp.resource_list_changed`
  - `policy_activated`
  - `interruption`, `substitution`
  - `agent_forked`, `run_started`, `run_finished`, `snapshot_materialized`
- ToolCallStatus
  - `ok`, `error`, `timeout`, `canceled`, `interrupted`
- InterruptionReason
  - `crash`, `shutdown`, `context_reset`, `approval_denied`, `timeout`, `canceled`, `system_restart`
- ResourceKind
  - `docker_volume`, `docker_container`, `docker_image`, `file`, `uri`
- MessageRole
  - `system`, `user`, `assistant`, `tool`
- ResourceLiveness
- `unknown`, `alive`, `dead`

## Resource primitives and capabilities

Many resources fall into two buckets:

- Snapshot/fork/restore capable (controlled): e.g., Docker volumes, policy files, linear logs.
- Imperative handles (not restorable): e.g., a running container’s RAM/process state.

Model with explicit capabilities so forks/time travel are well‑defined.

Capabilities (set of strings)

- `snapshot`, `restore`, `fork`, `share`, `migrate`, `checkpoint`

Scope and leases

- `scope` associates resources with `{agent_id, run_id, session_id}`; leases/liveness are advisory and non‑deterministic.

## Core logical types

Message (OpenAI Responses API compatible)

- `Message` (Node)
  - `id`: NodeId
  - `role`: MessageRole
  - `content`: list of content parts (see below)
  - `created_at`: ts
- `ContentPart` (discriminated by `type`)
  - `text`: `{type: "text", text: str}`
  - `image_url`: `{type: "image_url", url: str, detail?: str}`
  - `tool_use`: `{type: "tool_use", id: str, name: str, input: dict}`
  - `tool_result`: `{type: "tool_result", tool_call_id: str, output: any, is_error?: bool}`
  - `reasoning`: `{type: "reasoning", text: str}` (optional when model emits separate reasoning)

Model I/O

- `ModelRequest` (Event: `model_request`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `model`: str
  - `params`: dict (temperature, max_tokens, etc.)
  - `input_refs`: list[EventRef or NodeId of Message]
  - `tools_spec?`: dict (as given to the model)
  - `correlation_id`: str
- `ModelResponse` (Event: `model_response`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `request_id`: str
  - `output_ref`: NodeId (Message)
  - `usage?`: `{prompt_tokens:int, completion_tokens:int, total_tokens:int}`
  - `finish_reason?`: str
  - `reasoning_ref?`: NodeId (Message) if present
  - `stream_ref?`: ExtRef to a streaming artifact (e.g., path or external id)

Agent history

- `HistoryAppend` (Event: `history_append`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `list`: str (e.g., `main`)
  - `message_ref`: NodeId (Message)

MCP notifications

- `McpResourceUpdated` (Event: `mcp.resource_updated`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `server`: str
  - `uri`: str
  - `etag?`: str, `hash?`: str
  - `summary?`: str
- `McpResourceListChanged` (Event: `mcp.resource_list_changed`)
  - `event_id`, `ts`, `run_id`, `agent_id`, `server`: str

Tools

- `ToolCall` (Event: `tool_call`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `server`: str (e.g., `runtime`)
  - `tool`: str (e.g., `exec`)
  - `call_id`: str
  - `args`: dict (tool‑specific, e.g., for exec: `{cmd: str|list, cwd?: str, timeout_ms?: int, env?: dict}`)
  - `requested_by`: EventRef (usually a `model_response`)
- `ToolResult` (Event: `tool_result`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `call_id`: str
  - `status`: ToolCallStatus
  - `result`: dict (tool‑specific; for exec: `{rc:int, stdout:str, stderr:str, duration_ms?:int}`)
  - `error?`: `{code:str, message:str, details?:any}`

Resource lifecycle

- `SnapshotCreated` (Event: `snapshot_created`)
  - `event_id`, `ts`, `agent_id`
  - `resource_ref`: NodeId (Resource)
  - `snapshot_ref`: NodeId (Snapshot)
  - `outcome`: `ok` | `error` (optional `error` payload)
- `ResourceAttached` (Event: `resource_attached`)
  - `event_id`, `ts`, `agent_id`, `run_id?`, `resource_ref`: NodeId (Resource), `mode`: `ro` | `rw`
- `ResourceDetached` (Event: `resource_detached`)
  - `event_id`, `ts`, `agent_id`, `run_id?`, `resource_ref`: NodeId (Resource)
- `ResourceLivenessProbe` (Event: `resource_liveness_probe`)
  - `event_id`, `ts`, `resource_ref`: NodeId (Resource), `status`: ResourceLiveness
- `RestoreAttempted` (Event: `restore_attempted`)
  - `event_id`, `ts`, `agent_id`, `snapshot_ref`: NodeId (Snapshot), `outcome`: `ok` | `unsupported` | `error`, `new_resource_ref?`: NodeId (Resource)

Approval policy

- `Policy` (Node)
  - `id`: NodeId
  - `kind`: `active` | `proposal`
  - `path_or_uri`: PathRef or UriRef (e.g., `state/policy/current/policy.py` or `approval-policy://policy.py`)
  - `docstring?`: str
  - `tests_summary?`: `{total:int, passed:int, failed:int}`
  - `source_digest?`: str (hash of source code)
- `PolicyActivated` (Event: `policy_activated`)
  - `event_id`, `ts`, `run_id?`, `agent_id`
  - `policy_ref`: NodeId (Policy)
  - `notes?`: str

Resources (runtime volumes/containers/images)

- `Resource` (Node)
  - `id`: NodeId
  - `kind`: ResourceKind
  - `name`: str
  - `capabilities`: list[str] (see Capabilities)
  - `imperative?`: bool (true when not restorable)
  - `scope?`: `{agent_id?:str, run_id?:str, session_id?:str}`
  - `liveness?`: `{status: ResourceLiveness, last_probe_ts?: ts}`
  - `metadata?`: dict
- `Snapshot` (Node)
  - `id`: NodeId
  - `resource_ref`: NodeId (Resource)
  - `method`: str (e.g., `docker_volume_tar`, `oci_commit`, `fs_copy`, `app_checkpoint`)
  - `external_ref`: ExtRef (e.g., tar digest, OCI digest, S3 key)
  - `consistency?`: `crash_consistent` | `app_consistent`
  - `size_bytes?`: int, `created_at`: ts, `restorable?`: bool
- `Handle` (Node)
  - `id`: NodeId
  - `resource_ref`: NodeId (Resource)
  - `provider`: str (e.g., `docker`)
  - `provider_id`: str (opaque id, e.g., container id)
  - `restorable`: false
  - `capabilities?`: list[str] (e.g., `attach`, `exec`, `kill`, `logs`)

Forks, subagents, interruptions

- `AgentForked` (Event: `agent_forked`)
  - `event_id`, `ts`, `agent_id`
  - `from_branch`: str, `to_branch`: str
  - `at_event`: EventRef
  - `volume_plan?`: list of `{resource: NodeId, mode: keep|snapshot|discard, kept_by?: str, snapshot_ref?: ExtRef}`
- `Subagent` (Node)
  - `id`: NodeId
  - `parent_agent_id`: AgentId
  - `constraints?`: dict
- `Interruption` (Event: `interruption`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `reason`: InterruptionReason
  - `affects`: list[EventRef]
- `Substitution` (Event: `substitution`)
  - `event_id`, `ts`, `run_id`, `agent_id`
  - `original_event`: EventRef
  - `replacement_event`: EventRef
  - `reason?`: str

Runs and snapshots

- `RunStarted` (Event: `run_started`)
  - `event_id`, `ts`, `run_id`, `agent_id`, `reason?`: str (user|schedule|subagent)
- `RunFinished` (Event: `run_finished`)
  - `event_id`, `ts`, `run_id`, `agent_id`, `exit?`: str (success|error), `summary_ref?`: ExtRef
- `Snapshot` (Node; cache only)
  - `id`: NodeId
  - `run_id?`: RunId
  - `content_ref`: ExtRef (materialized summary/transcript for UI)

## Edge types (typed relations)

- `AGENT_HAS_RUN`: Agent → Run
- `RUN_HAS_EVENT`: Run → Event (attr `seq:int` to preserve order)
- `EVENT_PRODUCES`: Event → {Message|Generation|ToolResult|Summary|Snapshot}
- `EVENT_CONSUMES`: Event → {Message|Generation|ToolCall|Resource}
- `APPENDED_TO_HISTORY`: Run/Agent → Message (attr `list:str`)
- `CALLS_TOOL`: Event/Generation → ToolCall
- `RETURNS_RESULT`: ToolCall → ToolResult
- `MCP_RESOURCE_UPDATED`: Event → Resource (attr `uri:str`)
- `INTERRUPTS`: Event → Event (interrupted)
- `SUBSTITUTES`: Event → Event (replacement_of)
- `SUMMARIZES`: Summary → {Message|Event|Run}
- `DERIVED_FROM`: Any derived node → sources
- `OWNS_RESOURCE`: Agent/Run → Resource (ownership over time)
- `MOUNTS_RESOURCE`: Run/Container → Resource (attrs `{mount:str, mode:ro|rw}`)
- `HAS_SUBRESOURCE`: Resource → Resource (decomposition)
- `SNAPSHOT_OF`: Snapshot → Resource
- `RESTORES_TO`: Snapshot → Resource (new instance created by restore)
- `ATTACHED_TO`: Resource → {Run|Agent} (attrs `{mode:ro|rw}`)
- `FORKED_FROM`: Branch/Subagent → {Agent|Run|Event}
- `SPAWNED_FROM`: Subagent → Agent
- `ACTIVATES_POLICY`: Event → Policy
- `PROPOSES_POLICY`: Event → Policy

## Invariants and notes

- Idempotence: event ids derive from canonical JSON; re‑emitting identical events yields the same `EventId`.
- Full payloads: no redaction or MIME/size limits in the canonical model (backends may impose practical limits).
- Ancestry: per‑run event streams are linear; linkability ensured via `RUN_HAS_EVENT.seq` and optional `parent` pointers in event payloads when available.
- Correlation: related actions share a `correlation_id` (e.g., `model_request` ↔ `model_response` ↔ tool chain).
- Approval policy:
  - Active policy is a program (stdin→stdout JSON) stored behind the MCP resource `resource://approval-policy/policy.py`.
  - Activation is represented by a `policy_activated` event and a Policy node updated to `kind=active`.
- Volumes/resources: payloads of volumes are not stored in the graph; represent lineage via `Resource` nodes and `ExtRef` snapshots only.
- Imperative resources: when `imperative=true` or capabilities omit `restore`, forks must choose `keep` (single branch), `snapshot` (if supported) or `discard`. Attempting `restore` on such resources yields `restore_attempted` with `outcome=unsupported`.
- Subresources: use `HAS_SUBRESOURCE` to decompose complex resources (e.g., container → filesystem volume, network namespace) and make fork decisions per subpart.
- Liveness: liveness probes are advisory; only snapshots and events are durable.

## Minimal JSON examples (illustrative)

**Model request:**

```json
{
  "kind": "model_request",
  "event_id": "...",
  "ts": "...Z",
  "run_id": "...",
  "agent_id": "...",
  "model": "o4-mini",
  "params": { "temperature": 0.2 },
  "input_refs": [{ "event_id": "prev-msg-event-id" }],
  "correlation_id": "abc123"
}
```

**Tool result (exec):**

```json
{
  "kind": "tool_result",
  "event_id": "...",
  "ts": "...Z",
  "run_id": "...",
  "agent_id": "...",
  "call_id": "exec-42",
  "status": "ok",
  "result": { "rc": 0, "stdout": "...", "stderr": "", "duration_ms": 120 }
}
```

**Snapshot created:**

```json
{
  "kind": "snapshot_created",
  "event_id": "...",
  "ts": "...Z",
  "agent_id": "...",
  "resource_ref": "res-vol-123",
  "snapshot_ref": "snap-abc",
  "outcome": "ok"
}
```

**Restore attempted (unsupported):**

```json
{
  "kind": "restore_attempted",
  "event_id": "...",
  "ts": "...Z",
  "agent_id": "...",
  "snapshot_ref": "snap-live-ram",
  "outcome": "unsupported"
}
```

## Mapping helpers (backend adapters)

Backends should provide adapters to:

- Emit/ingest events of the above kinds (content‑hashing for `EventId`).
- Persist/lookup nodes and edges with their typed attributes.
- Provide convenient queries:
  - History (events by run in order),
  - Lineage (DERIVED_FROM chains),
  - Tool call/result correlation,
  - Resource ownership/mounts at time t,
  - Policy proposals/activation history,
  - Fork/subagent ancestry.
