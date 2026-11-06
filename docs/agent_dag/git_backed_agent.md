# Git-backed agent state and PR workflow (concept)

This proposal makes a per-agent Git repository canonical for mutable policy, templates,
presets and an event journal. Reconstruct prompts, pending approvals and UI state from
the commit sequence at `HEAD`; forks share that history and propose changes through PRs.
The `adgn` paths below are proposed integration points, not implemented APIs.

## Recommended pattern

The [canonical schema](schema.md#design-principles-and-pattern) owns the shared
functional-core/controller, spec/status, correlation and resource-capability rules.
For this Git realization:

- Keep runtime code and server wiring read-only in the container; mutable policy,
  SBPL templates and presets live in the agent repository and activate through validation.
- Record user edits, policy activation, model I/O, tool calls/results and proposal
  creation/deletion. Batch related events in commits; do not commit every streamed token.
- Store large or repeated payloads once by content ID. Events carry references;
  Git blobs/packfiles share content, with optional chunking/Merkle trees for large bodies.
- Treat materialized indexes as caches; reconstruction uses the canonical history.
- Host-side controllers enact intents and record outcomes, including partial failure and retries.
- Use Git PRs for code/policy changes; graph changes may instead use branch PRs or CAR proposals.

## Proposals as pull requests

Host agent state on shared Gitea with scoped credentials, private networking and a cap
on agent-authored open PRs. The existing [PR gate](../../x/gitea/pr_gate/README.md)
provides a candidate quota boundary; integrating it remains an open task.

Proposals live in `state/policy/proposals/<id>/`. A PR against
`state/policy/current/` validates the stdin→stdout JSON policy program, its docstring
and tests. After merge, a hook updates the active source and manifest, activates it
through the approval policy server and records `policy_activated` with its content ID.
Activation remains validated; a merge alone does not enact runtime effects.

The policy server exposes `resource://approval-policy/policy.py` read-only and
broadcasts `ResourceUpdated` on activation. Record MCP notifications and
`runtime/exec` tool calls/results with their correlation IDs.

## Subagents for long-context tasks

Subagents read repository snapshots/raw transcripts and commit summaries or indexes.
A model-runner sidecar can watch model-request or summarization commits and write
responses as new commits. Each summary retains source references; the UI/prompt can
use compact indexes without losing the underlying audit trail.

## Core data types (minimum viable)

Use the [backend-agnostic schema](schema.md#core-logical-types) for events and relations.
Serialize hashed JSON canonically (JCS/RFC 8785 or an explicitly chosen sorted-key,
UTF-8 encoding without insignificant whitespace). This Git layout adds content objects:

| Item                      | Git-specific representation                                                                                                                          |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| IDs                       | Stable `AgentId`; `RunId`/optional conversational `SessionId`; content-hashed `EventId` and `ObjectId`; logical `VolumeId` for runtime volume names. |
| Event envelope            | `event_id`, previous-event `parent`, UTC `ts`, `kind`, typed `refs`, small `payload`; the canonical schema owns correlation fields.                  |
| `ModelMessage`            | OpenAI-compatible role/content parts under `objects/messages/<ObjectId>.json`; bodies may include text, tool use/results and separate reasoning.     |
| `ModelRequest`            | `request_id`, model/params, `input_message_refs`, `system_refs`, optional `tools_spec`, `correlation_id`.                                            |
| `ModelResponse`           | `request_id`, `output_message_ref`, usage/finish reason and optional `reasoning_ref`/`stream_ref`.                                                   |
| `HistoryAppend`           | Message/result content ID and target history list, such as `main`.                                                                                   |
| `ToolCall` / `ToolResult` | Server/tool, `call_id`, `requested_by`; `args_ref` or `result_ref` to large bodies, with status/error.                                               |
| MCP notifications         | Server/URI and optional etag, hash or summary reference.                                                                                             |
| Active policy             | `state/policy/current/policy.py`; activation event references its source `ObjectId`.                                                                 |
| Proposal                  | `state/policy/proposals/<id>/policy.py` plus optional author/date/title metadata.                                                                    |
| `AgentFork`               | `from_branch`, `to_branch`, `at_event`, and `volume_plan` entries with `volume`, `mode: keep                                                         | fork | discard`, `kept_by`or`snapshot_ref`; the canonical schema calls the snapshot choice `snapshot`. |
| `VolumeSnapshot`          | Logical volume, creation time, size estimate, driver/method and external tar/OCI/S3 identifier; no volume payload in Git.                            |
| Run boundaries            | Start reason (`user`, `schedule`, `subagent`); finish status and optional summary reference.                                                         |
| UI `Snapshot`             | Rebuildable prompt/policy/proposal/transcript view under `indexes/`.                                                                                 |

Record full payloads as-is, without redaction or filtering in this conceptual model.
The normalized object references above are Git storage choices, not replacements for
the canonical event vocabulary.

## Repository layout (concrete)

```text
events/<YYYY-MM>/<sequence>_<EventId>.json  # small, human-diffable events
runs/<RunId>/transcript.jsonl
runs/<RunId>/summary.json                  # optional
objects/messages/<ObjectId>.json
objects/tool-results/<ObjectId>.json
objects/streams/<ObjectId>.jsonl           # optional streaming captures
objects/misc/<ObjectId>.json               # typed by kind
state/policy/current/policy.py
state/policy/current/manifest.json         # source ObjectId, activation time
state/policy/proposals/<id>/policy.py
state/policy/proposals/<id>/metadata.yaml
state/seatbelt-exec/sbpl-templates/<name>.json
indexes/latest.json                       # cached recent events/heads
indexes/snapshots/<RunId>.json
meta/agent.yaml                           # AgentId, defaults, server names
meta/branches.yaml                        # forks and resource owners
```

Keep events diffable; mark payload objects binary/non-text in `.gitattributes`.
Large streams/tool results can use LFS or external storage with content-hash pointers.

## Git mapping and workflow

- `main` or `agent/<name>` holds primary linear history; `fork/<id>-<purpose>`
  records a fork; `feat/*` and `policy/<proposal-id>` hold proposals/experiments.
- Batch a related request/response/history/tool chain into a commit. Suggested message:
  `event: <kind> [<run_id>]`, with a summary and `Refs:` content-ID footer.
- Tag boundaries as `run/<RunId>/start` and `run/<RunId>/end`; materialize compact
  transcripts under `runs/`. A Git sidecar stages, commits, tags and pushes.
- Policy merge/activation and MCP recording follow [the proposal workflow](#proposals-as-pull-requests).

## Forking and volumes

Volumes remain outside Git. After a fork, exactly one branch can keep a live,
non-restorable volume; others snapshot/fork it where supported or discard it.
Record the plan in the fork event and `meta/branches.yaml`; snapshots carry external
reconstruction references. A merge must resolve competing ownership out of band
and record the winner in its merge commit.

## Interruption and substitution semantics

An interruption (crash, shutdown, approval denial or context reset) references affected
in-flight requests/tool calls. Replacement work emits `substitution` with original,
replacement and reason. Later events reference the replacement while retaining that lineage.
Identical canonical payloads produce identical event IDs, allowing idempotent re-emission.

## Realization options and recommendation

The proposed initial `adgn` realization is pure Git: canonical JSON/content hashes,
event/object writers and a thin Git sidecar. It preserves ordinary PR/fork workflows
without another store. Add LFS/git-annex for large objects if needed; alternatively,
keep small events and content-hash pointer files in Git while moving large bodies
to S3, CAR files or an OCI registry. Policy activation stays with the approval policy
MCP server; no host volume mirrors are proposed.

## Graph-first state (property graph)

An alternative makes the property graph canonical and projects append-only events
into nodes/edges with validity windows. Git then manages mutable code/templates and
optional exports. Full payloads remain unfiltered; large streams can be external files.

Use the [canonical node/event types](schema.md#core-logical-types) and
[edge inventory](schema.md#edge-types-typed-relations). Additional conceptual nodes carry:
`Agent(id, name, metadata)`, `Run(run_id, agent_id, started_at, finished_at?)`,
`Event(event_id, ts, kind, payload or payload_path)`,
`Generation(model, params, usage, input_ref, output_ref)`, `Summary(scope, method, quality)`
and `AgentFork(from, to, split metadata)`.
This projection maps `FORKED_FROM` from `AgentFork`/`Branch` to `Agent`/`Run`/`Event`;
the canonical schema instead starts it from `Branch`/`Subagent`. Preserve that distinction.

- Nodes/edges carry optional `valid_from`/`valid_to` windows for as-of-time queries.
  A fork creates a branch head, shares unchanged nodes and records resource decisions.
- `Summary` uses `SUMMARIZES`/`DERIVED_FROM`; subagents use `SPAWNED_FROM` and
  read a `Snapshot`/`Run`. Resources use `MOUNTS_RESOURCE` and `OWNS_RESOURCE`
  to track mount mode, ownership and handoffs over time.
  This sketch treats subagents as `Agent` nodes; the canonical schema has a `Subagent` node.
- Project model I/O into `Generation`, runtime exec into calls/results, policy merge
  into `ACTIVATES_POLICY`, and interruptions/replacements into their typed edges.
- Candidate stores: Neo4j/Cypher, ArangoDB graph/documents, or SQLite `nodes`/`edges`
  tables with JSON and validity columns. These are alternatives, not selected dependencies.

Proposed layout: graph at `var/agent_graph.db` (or a server), streams at
`var/artifacts/<run_id>/*`, Git code under `state/`, optional JSONL exports at
`exports/graph-snapshot-<ts>.jsonl`. Implement typed Pydantic nodes/edges, a `GraphStore`
for upserts/validity/event projection (SQLite first), ingestion hooks and queries for ordered run
events, generation→tool results, forks and ownership at time t. JSONL import/export
provides the proposed portability/backup path.

## Single-store with VCS-like semantics

Other proposed stores combine code and state with branching/merging:

| Candidate  | Mapping and tradeoff                                                                                                                                                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Dolt       | SQL `nodes`, `edges`, and `files(path, content, meta)` tables; branches hold proposals/state, diffs reconcile merges. Familiar SQL/versioned workflows, but a graph must be modeled in tables and operations differ from PostgreSQL. |
| TerminusDB | Nodes/edges and path-keyed code documents, versioned branches for proposals/forks. Graph/versioning APIs; different tooling and a smaller ecosystem than the alternatives considered here.                                           |
| LakeFS     | Code/events under versioned object prefixes and PRs. Suits large artifacts; requires a separate query index such as SQLite/ClickHouse.                                                                                               |

For the Dolt sketch, `nodes(id, kind, data, valid_from, valid_to)` and
`edges(id, src, dst, kind, data, valid_from, valid_to)` retain typed graph data.
Policy branches update `files`; merge hooks update the active MCP resource. State
may share those branches or use dedicated branches; queries select the desired commit.

## Other alternatives

IPLD/IPFS or OSTree offer content-addressed storage; event sourcing can pair
EventStoreDB/Kafka/Pulsar with an object store and JSON Patch/Merge Patch deltas.
Noms offers another versioned-data option; DVC/DataLad/git-annex keep large
content outside Git. Automerge/Y.js are candidates if true concurrent writers need
CRDT convergence. Content-defined chunking (Rabin/Buzhash) and BLAKE3 trees are
possible large-payload deduplication techniques, not prerequisites for the initial design.

## IPLD/IPNS integration

The [IPLD overview](ipld/overview.md) owns heads/config, CAR proposals, Git interlinking
and read-only container integration; [Python notes](ipld/python.md) cover its client path.

## Canonical schema

[schema.md](schema.md) owns the shared logical types, relations and invariants for all realizations.

## Implementation plan (adgn)

These phases remain proposals:

1. Define journal/object Pydantic models under `adgn/agent/journal/models.py`, with
   `to_canonical_json()`/`content_hash()`, `JournalWriter` and `GitSidecar`.
2. Expose policy proposals through MCP, validate docstring length/`TEST_CASES` in CI and
   activate merged policies through the policy server, recording successful activations.
3. Add fork events, branch ownership and external snapshot metadata/helpers.
4. Record MCP/tool correlation and interruption/substitution semantics.
5. Expose cached UI snapshots and a transcript-summary subagent with a defined trigger.

Open work includes final layout/schema selection, PR validation/activation hooks,
PR-gate integration with scoped credentials and a summarization prototype. Validate
canonical-hash idempotence, schema checks and incremental reconstruction at `HEAD`,
plus the repository's lint/type checks. The graph alternative would instead start
with the typed store/projection/query layer described above.
