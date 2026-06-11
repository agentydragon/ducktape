# Git‑Backed Agent State and PR Workflow (Concept)

This document explores using a git repository as the canonical source of truth for an agent’s mutable state (policy, templates, presets, event journal), enabling deterministic reconstruction of agent prompts/state and first‑class proposals as pull requests.

## Rationale

- Determinism and auditability: Every meaningful event (user edits, policy activation, model responses, tool calls/results, proposal create/delete) is a commit. The agent prompt, pending approvals, and UI state can be reconstructed from the linear commit sequence at HEAD.
- Forkability: “Fork agent here” becomes trivial — fork the repository and all state follows.
- Collaboration and review: Proposals and other changes use normal PR workflows, with human review/approval and automated validation.

## Recommended Pattern

- Functional core, imperative shell
  - Keep immutable facts in a DAG (events, snapshots, heads) for time‑travel and forks.
  - Use host‑side controllers to enact intents (tools, pins, publishes) and write back outcomes; expect retries and partial failure.
- Spec vs status (controller/operator)
  - Intents/specs are events/proposals; outcomes/status are tool_results, liveness probes, snapshot_created, restore_attempted.
- Resource strategy per fork/time‑travel
  - For each resource: keep | snapshot | discard, driven by capabilities. Live, non‑restorable handles can be kept by one branch or discarded; volumes/files are snapshotted/restored.
- Idempotency and correlation
  - Stable ids and correlation across request→response chains; design controllers to be idempotent.
- Approvals
  - Code via Git PRs (policy/templates). Graph/data via branch/PR or CAR‑based proposals depending on backend. Merge → activate/publish + record.

## Proposals as pull requests

- Host agent state repos on a shared Gitea. The agent authors proposals as branches and opens PRs via standard flows; merges become activation events.
- See `gitea_pr_gate/`: a small gate that limits the maximum number of open PRs to prevent spam. Give the agent scoped Gitea credentials, run on a private network, and enforce caps.
- This approach allows remote durability, auditability, and trivial branching/experimentation, while keeping the runtime container as the execution sandbox.

## Immutable vs. mutable scaffold

- Immutable bits: core runtime code and infrastructure (the `adgn` package, server wiring). Versioned like normal software and mounted read‑only into the runtime container.
- Mutable bits: approval policy, SBPL templates, presets/configuration. These live in the agent’s repo (the per‑agent state repository) and are runtime‑live‑swappable (with safety gates: docstring + tests) via PRs.
- The runtime mounts the agent’s repo and reads mutable bits from the current HEAD. Policy activation (post‑merge) remains validated before use.

## Subagents for long‑context tasks

- With all state in a git repo, the agent can spawn subagents (e.g., via the OpenAI API) to operate on repository snapshots and summarize long histories that exceed its context window. Subagents read raw logs/transcripts from the repo, produce condensed summaries or indices, and write them back as commits.
- This enables hierarchical workflows (primary agent delegates long‑context summarization, indexing, or analytics), keeps the primary agent’s context lean, and preserves a deterministic audit trail. A dedicated sidecar (with OpenAI access) can watch for “summarize” commits and materialize results as commits.

## Functional/persistent modeling on top of git’s DAG

Git already gives us a persistent, content‑addressed Merkle DAG: commits (nodes) point to trees and blobs (content). We can lean into this to model agent state as a functional data structure. Instead of duplicating large, repeated values (e.g., the same “message structure” appearing in many turns), we store the content once (as a blob) and reference it by hash. Successive commits then evolve only the minimal set of references (structural sharing), keeping history compact.

Practically, this means event payloads can be normalized to reference large substructures (messages, tool results, traces) by content IDs. Small JSON events embed references; the actual message bodies live in a stable object path (or as raw blobs with an index). Git’s object store and packfiles naturally deduplicate repeated content across commits; for very large blobs, we can chunk into a small Merkle tree to improve delta packing and random access. The result is a functional, append‑only state machine where “the same structure in 37 places” is stored once and shared everywhere it’s referenced.

On top of that, we can maintain materialized indexes (denormalized views) for fast reads while treating them strictly as caches. Reconstruction flows always consult the canonical DAG at HEAD, ensuring determinism. This model also composes with PRs and subagents: PRs change references (not duplicated content), and subagents can safely read shared blobs and write new summarized blobs whose identities are content‑derived.

## High‑level sketch (optional next steps)

- Journal: define an `events/` folder where each commit adds a structured event file (JSON/JSONL) with a monotonic sequence and timestamp. Materialize `runs/<id>/transcript.jsonl` on run finish.
- Policy and templates: keep under `state/policy/current/` and `state/policy/proposals/`, and `state/seatbelt-exec/sbpl-templates/` respectively. PRs propose changes; merges activate.
- Sidecars: a git sidecar (for staging/committing/pushing) and an optional model‑runner sidecar (watches for “model_request” events and commits “model_response” results).
- Deterministic reconstruction: rebuild prompt/UI from HEAD; `indexes/` serve as caches only.

## Open questions / TODOs

- [ ] Finalize repo layout and event schemas (pydantic‑validated).
- [ ] Define PR validation gates (docstring length, TEST_CASES pass) and CI hooks.
- [ ] Integrate `gitea_pr_gate/` for PR caps and scoped creds.
- [ ] Prototype “summarize long history” subagent and sidecar flow.

## Other alternatives

If git‑backed isn’t the right fit, consider these standard patterns:

- Content‑addressed DAGs
  - IPLD/IPFS (local blockstore or full IPFS): model state as IPLD objects with typed links; bundle snapshots in CAR files. Pros: native dedupe, schemaable DAG; Cons: adds blockstore/runtime.
  - OSTree: versioned filesystem trees with content addressing; good when state maps cleanly to trees/blobs.
- Event sourcing + object store
  - EventStoreDB (or Kafka/Pulsar) for append‑only event log; S3‑style object store for large blobs keyed by content hash. Pros: CQRS/event‑sourcing workflow; Cons: extra infra vs git‑only.
  - JSON Patch / JSON Merge Patch for deltas; pair with content‑addressed blobs to dedupe repeated substructures.
- “Git for data” and data‑versioning tools
  - Dolt / Noms: structured, content‑addressed DBs with commits/branches and PR‑like flows.
  - DVC / DataLad / git‑annex: pointers in git, large content in remote/object store (keeps PR UX, avoids bloating git).
- CRDTs for true multi‑writer
  - Automerge / Y.js: converge concurrent edits of JSON docs; likely overkill for append‑only, but useful if multiple writers are unavoidable.
- Dedup techniques
  - Content‑defined chunking (Rabin/Buzhash) and BLAKE3 tree hashing to chunk/hash big payloads and coalesce repeats across events.

## Event tracking examples

The journal should let us answer “what happened and how did it affect state?” with chainable references. Typical lineage:

1. An OpenAI request uses these input messages and yields output message `X`.
   - Event: `model_request { message_refs: [ids], model, params }`
   - Event: `model_response { message_id: X, usage, reasoning? }`
2. Message `X` is appended to the agent history list.
   - Event: `history_append { message_id: X, list: "main" }`
3. The agent creates a Docker exec command and sends it to the docker MCP server under this linear session.
   - Event: `tool_call { server: "runtime", tool: "exec", args_json, call_id }`
4. The docker server returns output `Y`.
   - Event: `tool_result { call_id, result_ref: Y }`
5. Output `Y` is appended as another message into context.
   - Event: `history_append { message_id: Y, list: "main" }`

Suggested common fields

- `event_id` (content hash), `parent` (prev event), `ts` (ISO‑8601), `kind` (enum), `refs` (content ids: messages, blobs), and `payload` (small JSON). Large bodies go into content‑addressed blobs to enable dedupe; events carry references.

## Core data types (minimum viable)

This section defines the logical types we need to represent and how they relate. In practice, these should be implemented as Pydantic models and serialized with a canonical JSON encoding for hashing (e.g., IETF JCS/RFC‑8785 or an `orjson` config with sorted keys, UTF‑8, and no insignificant whitespace).

Identifiers

- `AgentId` (string): stable id for an agent instance (scopes Docker volume names).
- `RunId` (string/UUID): one interactive session or job from start to end.
- `SessionId` (string/UUID): conversational grouping within a run (if needed).
- `EventId` (content hash): deterministic id of an event JSON payload.
- `ObjectId` (content hash): deterministic id for large/structured content stored under `objects/`.
- `VolumeId` (string): logical name for Docker named volumes (derived via `adgn.agent.runtime.volumes`).

OpenAI Responses API

- `ModelMessage` (object)
  - `role` (system|user|assistant|tool)
  - `content[]` items following Responses API shapes (e.g., `input_text`, `output_text`, `tool_use`, `tool_result`).
  - Stored as an object in `objects/messages/<ObjectId>.json` (canonically hashed).
- `ModelRequest` (event: `model_request`)
  - `request_id` (string), `model` (string), `params` (temperature, max_tokens, etc.)
  - `input_message_refs[]` (`ObjectId`), `tools_spec` (optional), `system_refs[]` (`ObjectId`)
  - `correlation_id` (string) to join with downstream results.
- `ModelResponse` (event: `model_response`)
  - `request_id`, `output_message_ref` (`ObjectId`), `finish_reason` (enum), `usage` (prompt/comp/total tokens)
  - Optional: `reasoning_ref` (`ObjectId`) if model returns separate reasoning content.
  - Optional streaming: `stream_ref` (`ObjectId`) pointing to a `streams/<id>.jsonl` capture.

Agent history and semantics

- `HistoryAppend` (event: `history_append`): appends a message or result into a named list (e.g., `main`), carrying an `ObjectId` reference.
- `McpResourceNotification` (event: `mcp.resource_updated` | `mcp.resource_list_changed`)
  - `server` (name), `uri` (string), optional `etag`/`hash` and `summary_ref` (`ObjectId`).
- `ToolCall` (event: `tool_call`)
  - `server` (e.g., `runtime`), `tool` (e.g., `exec`), `call_id` (string), `args_ref` (`ObjectId`), `requested_by` (`EventId`).
- `ToolResult` (event: `tool_result`)
  - `call_id`, `status` (ok|error|timeout|canceled), `result_ref` (`ObjectId`), optional `error` (code, message, details).
- `Interruption` (event: `interruption`)
  - `reason` (e.g., `crash`, `shutdown`, `context_reset`, `approval_denied`), `affects` (event ids).
- `Substitution` (event: `substitution`)
  - `original_event` (`EventId`), `replacement_event` (`EventId`), `reason` (string). Example: “substituted tool call due to interruption”.

Mutable code and templates

- `ApprovalPolicyActive` (file state + activation event)
  - Active policy program is stored under `state/policy/current/policy.py` and exposed via MCP as `resource://approval-policy/policy.py`.
  - Activation represented as an event: `policy_activated` with policy `ObjectId`.
- `ApprovalPolicyProposal` (directory state + PR)
  - `state/policy/proposals/<id>/policy.py`, optional `metadata.yaml` with author/date/title.
  - Proposal lifecycle via PRs; merge triggers activation validation.
- `SeatbeltTemplates` (files)
  - Managed by the seatbelt MCP server; no runtime volume mirrors.

Forks and runtime linkage

- `AgentFork` (event: `agent_forked`)
  - `from_branch`, `to_branch`, `at_event` (`EventId`), and a `volume_plan[]` with entries `{volume: VolumeId, mode: keep|fork|discard, kept_by: branch}`.
  - If volumes are kept by one branch, record `kept_by` to indicate ownership; if forked, record `snapshot_ref` describing how to reproduce the fork (tarball digest, OCI layer ref, or external ID).
- `VolumeSnapshot` (object)
  - Metadata only: `{volume: VolumeId, created_at, bytes_estimate, driver, snapshot_kind}` with `external_ref` (OCI layer digest, S3 key, or Docker‑volume‑snapshot id). Do not store the payload in git.

Runs and snapshots

- `RunStarted`/`RunFinished` (events)
  - `run_id`, `reason` (user|schedule|subagent), optional `exit` (success|error) and `summary_ref`.
- `Snapshot` (object)
  - Materialized view for UI: prompt state, policies, proposals, and a compact transcript. Treated as a cache under `indexes/` and regenerated as needed.

Recording policy

- Full payloads are recorded as-is; no redaction or filtering is applied.

## Repository layout (concrete)

Recommended layout that balances dedupe, readability, and git ergonomics:

```
events/                                  # append-only event log (small JSON files)
  2024-10/                                # monthly shards to keep dirs small
    000001_<EventId>.json
    000002_<EventId>.json
runs/
  <RunId>/
    transcript.jsonl                      # compact linear view for this run
    summary.json                          # optional run summary object
objects/                                  # content-addressed store (JSON blobs)
  messages/
    <ObjectId>.json                       # ModelMessage bodies
  tool-results/
    <ObjectId>.json                       # structured tool outputs
  streams/
    <ObjectId>.jsonl                      # optional streaming captures
  misc/
    <ObjectId>.json                       # typed by a `kind` field
state/
  policy/
    current/
      policy.py                           # active policy program (stdin→stdout JSON)
      manifest.json                       # ObjectId, activation ts
    proposals/
      <id>/
        policy.py
        metadata.yaml                     # title, author, created, notes
  seatbelt-exec/
    sbpl-templates/
      <name>.json
indexes/                                  # caches only (rebuildable)
  latest.json                             # quick reference to last N events, heads
  snapshots/
    <RunId>.json
meta/
  agent.yaml                              # AgentId, model defaults, server names
  branches.yaml                           # extra metadata for forks/owners
```

Git attributes

- Mark `objects/` as binary (`.gitattributes` with `objects/* -diff -text`) to avoid noisy diffs on large blobs; keep events human‑diffable.
- Optionally enable Git LFS for `objects/streams/*` or very large `tool-results/*`.

## Git mapping and workflow

Branches and forks

- `main` (or `agent/<name>`): primary linear history.
- `fork/<id>-<purpose>`: branch created by `agent_forked` event. Record volume ownership in `meta/branches.yaml` and the fork event payload.
- `feat/<...>` / `policy/<proposal-id>`: working branches for proposals or experiments.

Commits strategy

- Small, frequent commits: batch related events (request→response→history appends→tool call→result) into a single commit where practical to avoid commit explosion on token streams; capture streams under `objects/streams/*` if needed.
- Commit message template: first line `event: <kind> [<run_id>]`, body with summary and refs. Include a `Refs:` footer listing `ObjectId`s for quick lookup.
- Tags: annotate run boundaries (`run/<RunId>/start`, `run/<RunId>/end`) for easy navigation.

PRs as approvals

- Proposals live in `state/policy/proposals/<id>/`. Opening a PR against `state/policy/current/` with that content triggers CI:
  - Lint for a valid policy program (stdin→stdout JSON).
  - On merge, a post‑merge hook updates `state/policy/current/policy.py`, updates `manifest.json`, and activates it via the approval policy server (broadcasting `ResourceUpdated`).
- Optional: record a `policy_activated` event with the `ObjectId` of the activated source.

MCP integration

- Record MCP resource notifications as events. The approval policy server broadcasts `ResourceUpdated` for the canonical URI `resource://approval-policy/policy.py`. The server also exposes this URI as a read‑only resource so clients can list/read the current policy text.
- Runtime `exec` calls are `tool_call`/`tool_result` pairs under server `runtime` and tool `exec`.

## Forking and volumes

Container volumes are not stored in git. Instead, describe their lineage:

- Ownership: after a fork, exactly one branch may “own” a live volume; others must fork snapshots or discard. Record this in `AgentFork.volume_plan` and `meta/branches.yaml`.
- Forked volumes: create a `VolumeSnapshot` object describing how to reconstruct (e.g., “OCI image digest sha256:... containing content on date X”).
- Reconciliation: when merging a fork back, any conflicting volume ownership requires out‑of‑band resolution; the merge commit should update `meta/branches.yaml` to reflect the winner.

## Interruption and substitution semantics

When a run is interrupted (crash, shutdown, approval denial), emit an `interruption` event listing affected in‑flight events (e.g., `model_request` or `tool_call`). If a subsequent action replaces the pending work, emit a `substitution` event that references `original_event` and `replacement_event`, with a reason like “substituted tool call due to crash”.

Guidelines

- Prefer idempotent writers: re‑emitting the same event with the same payload yields the same `EventId` (content hash), so duplicates are naturally deduped.
- Downstream events should reference the replacement event, not the original, but keep the lineage through `substitution` for auditability.

## Realization options and recommendation

Three viable realizations:

1. Pure git (recommended for adgn initial rollout)
   - Use the layout above, content‑addressed `objects/`, JSON events under `events/`, periodic `runs/*` transcripts.
   - Benefits: zero extra infra, great PR UX, easy forks, deterministic reconstruction. Combine with `gitea_pr_gate/` to cap agent‑authored PRs.
   - Caveats: consider Git LFS for very large payloads.

2. Git + LFS or git‑annex for large objects
   - Same structure, but store `objects/streams/*` and large `tool-results/*` in LFS to keep repo lean. Keeps PR UX intact.

3. Git pointers + external object store
   - Store only small event JSON in git; push large bodies to an object store (S3, CAR files, or OCI registry). `ObjectId` remains the content hash and a small pointer file indicates the external location. Useful when payloads are tens/hundreds of MB.

Recommendation for adgn

- Start with (1) Pure git, add LFS later if needed. Implement canonical JSON + content hashing, event writers, and a thin “git sidecar” for staging/committing/pushing.
- Align policy/seatbelt directories with runtime containerization; activations are handled via the approval policy MCP server (no host volume mirrors).

## Graph‑first state (property graph)

If you prefer time‑travel, forks, shared resources, and subagent workflows with first‑class graph semantics (without “git‑in‑git”), model the canonical index as a property graph and project events into nodes/edges. Git still manages mutable code (policy/templates) and optional exports.

Core idea

- Property graph is canonical; events are an append‑only log that project into nodes/edges with validity windows.
- Full payloads are recorded as‑is (no redaction). Very large streams can live as files; graph nodes reference their paths.

Nodes (examples)

- `Agent` (id, name, metadata)
- `Run` (run_id, agent_id, started_at, finished_at?)
- `Event` (event_id, ts, kind, payload inline or `payload_path`)
- `Message` (role, content, model attrs)
- `Generation` (model, params, usage, input_ref, output_ref)
- `ToolCall` (server, tool, args)
- `ToolResult` (status, result, error)
- `Resource` (docker volume/container/image; path/driver)
- `Policy` (active/proposal; path, commit, docstring, tests summary)
- `Summary` (scope, method, quality)
- `Snapshot` (materialized view pointer)
- `AgentFork` (from, to, split metadata)
- `Subagent` (spawned_from, constraints)

Edges (typed)

- `AGENT_HAS_RUN` Agent → Run
- `RUN_HAS_EVENT` Run → Event (`seq` attribute for order)
- `EVENT_CONSUMES` Event → Message/Generation/ToolCall/Resource
- `EVENT_PRODUCES` Event → Message/Generation/ToolResult/Summary/Snapshot
- `APPENDED_TO_HISTORY` Run/Agent → Message (list name)
- `CALLS_TOOL` Event/Generation → ToolCall
- `RETURNS_RESULT` ToolCall → ToolResult
- `MCP_RESOURCE_UPDATED` Event → Resource (uri)
- `INTERRUPTS` Event → Event (interrupted)
- `SUBSTITUTES` Event → Event (replacement_of)
- `SUMMARIZES` Summary → {Message|Event|Run}
- `DERIVED_FROM` Derived node → sources
- `OWNS_RESOURCE` Agent/Run → Resource (ownership)
- `MOUNTS_RESOURCE` Run/Container → Resource (mountpoint, mode)
- `FORKED_FROM` AgentFork/Branch → Agent/Run/Event
- `SPAWNED_FROM` Subagent → Agent
- `ACTIVATES_POLICY` Event → Policy
- `PROPOSES_POLICY` Event → Policy

Time travel and forks

- Bitemporal windows: nodes/edges carry `valid_from`/`valid_to` (open‑ended). Query state “as of t” with a temporal filter.
- Forks: create an `AgentFork` node at split; new Branch head; carry forward shared nodes until divergence. Record volume decisions (keep|snapshot|discard) on the fork node and via edges.

Summaries and subagents

- `Summary` nodes link via `SUMMARIZES` and `DERIVED_FROM`. Subagents are `Agent` nodes with `SPAWNED_FROM`; they read from a `Snapshot`/`Run` and write summaries back.

Shared resources

- Model runtime resources (e.g., Docker volumes) as `Resource`. Use `MOUNTS_RESOURCE` (mount, mode) and `OWNS_RESOURCE` (ownership) to track sharing and hand‑offs over time.

Storage options

- Neo4j (Cypher, ACID, easy traversals).
- ArangoDB (graphs + documents in one engine).
- SQLite property graph (lightweight): `nodes`/`edges` tables with JSON and validity columns; easy to ship and test.

Minimal on‑disk layout

- Graph store: `var/agent_graph.db` (SQLite) or a Neo4j/Arango instance.
- Artifacts/streams: `var/artifacts/<run_id>/*` with paths referenced from graph nodes.
- Git‑tracked code/config: `state/policy/current/policy.py`, `state/policy/proposals/*`, `state/seatbelt-exec/sbpl-templates/*`.
- Optional exports: `exports/graph-snapshot-<ts>.jsonl` (nodes/edges dump).

Illustrative queries (Cypher‑like)

```
// Events in a run at time t
MATCH (r:Run {id:$run})-[:RUN_HAS_EVENT]->(e:Event)
WHERE e.valid_from <= $t AND coalesce(e.valid_to, datetime('9999')) > $t
RETURN e ORDER BY e.seq

// Tool results derived from a generation
MATCH (g:Generation {id:$gid})<-[:EVENT_PRODUCES]-(:Event)
      -[:RETURNS_RESULT]->(tr:ToolResult)
RETURN tr

// Who owns a resource at time t
MATCH (a:Agent)-[o:OWNS_RESOURCE]->(v:Resource {id:$vid})
WHERE o.valid_from <= $t AND coalesce(o.valid_to,$t) > $t
RETURN a
```

Mapping to adgn

- Keep PR workflow for policies/templates; on merge, activate via the approval policy server and emit `ACTIVATES_POLICY`.
- Project OpenAI requests/responses into `Generation` and link with `EVENT_*` edges; `runtime/exec` becomes `ToolCall`/`ToolResult`.
- Represent interruptions/substitutions via `INTERRUPTS`/`SUBSTITUTES`; subagent spawns via `SPAWNED_FROM`.

Alternate implementation plan (graph)

- Add Pydantic models for Node/Edge types with `valid_from`/`valid_to` and typed refs.
- Implement `GraphStore` (SQLite first): upsert nodes/edges, set validity windows, and project events → graph.
- Add ingestion hooks in adgn to project OpenAI/MCP/tool events alongside current logging.
- Provide a small query layer for common traversals (history, forks, ownership at time t).
- Optional: export/import JSONL for portability and backups.

## Single‑store with VCS‑like semantics

If you want both “mutable code with PRs” and “agent state” in one system with branching/merging semantics, these databases provide Git‑style workflows without building a custom VCS:

- Dolt (SQL with Git semantics)
  - What: MySQL‑compatible DB with branches, commits, diffs, push/pull, and PR‑like flows.
  - Mapping: tables for `nodes` and `edges` (graph projection), plus a `files` table for code (`path TEXT PRIMARY KEY, content TEXT, meta JSON`). Use Dolt branches/merges for proposals and state evolution.
  - Pros: One store for code and state; branch/merge feels like Git; easy time travel (`dolt checkout <commit>`); SQL queries for analytics.
  - Cons: Not a native graph; you model graph on top of tables. Operationally different from Postgres.
  - Activation: runtime updates the MCP resource for the active policy (`resource://approval-policy/policy.py`).

- TerminusDB (versioned document/graph)
  - What: Document graph DB with built‑in versioning, commits, branch, diff, and WOQL/GraphQL APIs.
  - Mapping: store graph nodes/edges as documents; store code as documents keyed by path with content; use branches for proposals and forks.
  - Pros: Native graph + versioning; first‑class diff/branch/merge; good fit for time travel queries.
  - Cons: Different tooling than SQL; smaller ecosystem than Neo4j/Postgres.

- LakeFS (Git‑style on object storage)
  - What: Git‑like versioning over S3/GCS; branches/commits/PRs against object trees.
  - Mapping: store code and state/events as objects under a versioned prefix; use PRs for proposals and state updates.
  - Pros: Handles large artifacts naturally; PR UX for object trees.
  - Cons: Not a queryable DB; you’ll add a side index (e.g., SQLite/ClickHouse) for queries.

Adoption sketch (Dolt example)

- Schema
  - `nodes(id TEXT PRIMARY KEY, kind TEXT, data JSON, valid_from TIMESTAMP, valid_to TIMESTAMP NULL)`
  - `edges(id TEXT PRIMARY KEY, src TEXT, dst TEXT, kind TEXT, data JSON, valid_from TIMESTAMP, valid_to TIMESTAMP NULL)`
  - `files(path TEXT PRIMARY KEY, content TEXT, meta JSON)`
- Workflows
  - Policy edits: `dolt checkout -b policy/<id>`; update `files` rows; open PR; on merge, a hook updates the active policy resource.
  - State: project events into `nodes/edges` within the same branch or into dedicated state branches; forks create new branches; merges reconcile graph deltas using Dolt’s diff.
- Queries
  - Time travel: prefix queries with `/* at commit */` or `dolt_checkout <commit>` and use SQL over `nodes/edges`.

## IPLD/IPNS Integration

For the IPLD/IPFS path:

- Overview (heads/config, proposals, interlinking, container integration): ipld/overview.md
- Python integration notes: ipld/python.md

## Canonical Schema

Backend‑agnostic logical schema for nodes, events, and edges that all realizations should implement: schema.md

## Implementation plan (adgn)

Phase 1 — journaling and objects

- Add Pydantic models under `adgn/agent/journal/models.py` for events and objects. Include `to_canonical_json()` and `content_hash()` helpers (use JCS or `orjson` sorted keys).
- Implement a `JournalWriter` that writes `events/*`, `objects/*`, and `runs/*` (batching related events per commit) and a `GitSidecar` for add/commit/tag.

Phase 2 — policy workflows

- Materialize `state/policy/proposals/*` authoring via MCP tools; CI lints policy programs. Post‑merge hook updates `state/policy/current/*` and activates via the approval policy server.
- Emit `policy_activated` events on successful activation.

Phase 3 — forks and volume lineage

- Emit `agent_forked` events and maintain `meta/branches.yaml`. Add helpers for `VolumeSnapshot` metadata and external refs (do not store payloads in git).

Phase 4 — MCP semantics and interruptions

- Record `mcp.resource_*` events and ensure `runtime/exec` calls map cleanly to `tool_call`/`tool_result` with correlation ids. Emit `interruption`/`substitution` where applicable.

Phase 5 — UI and subagents

- Expose a compact `Snapshot` JSON under `indexes/` for Agent to render; add a summarization subagent that reads long transcripts and commits `summary` objects when thresholds are reached.

Testing and quality gates

- Add pytest suites for canonical hashing (idempotence), schema validation, and incremental reconstruction from events at HEAD. Ensure ruff + mypy clean.
