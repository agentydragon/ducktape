# History Service extraction, runner dial-out and delta settlement

Status: **in progress.** The [task DAG](task_dag.md#7-history-service-extraction-and-delta-settlement)
owns every step, its status and its edges; this plan owns the target, the facts the order rests
on, and the decisions behind it.

Three workstreams meet here: moving the raw event log and the thread fold into one History Service,
inverting runner connections, and settling streamed deltas. They touch the same ingestion path, so
they are sequenced together, but each can land in bounded steps without waiting for the others
except where an edge says so.

## Target

```mermaid
flowchart LR
    Browser --> App[Integration app: auth, browser API]
    App -- Sandboxes --> SS[Sandbox Service]
    App -- Sessions, commands --> GW[Runner Gateway]
    App -- thread and raw reads --> HS[History Service]
    Electric -- logical replication --> HSDB[(history database)]
    App -- auth proxy --> Electric
    Runner -- control stream: commands, receipts, lifecycle --> GW
    Runner -- pushes journal Events, acked by cursor --> HS
    HS --> HSDB
    GW --> GWDB[(sandbox_commands database: Sessions, command admission)]
```

| Component       | Owns                                                                                                                                    | Does not own                                                        |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Runner          | harness driving, journal, journal-ordered command admission, pushing its journal to one ingress                                         | durable history beyond its volume                                   |
| Sandbox Service | provisioning for every caller, lifecycle, grants, egress, teardown, the runner endpoints each Sandbox is created with                   | Sessions, commands, Events                                          |
| Runner Gateway  | the runner control stream and its authentication, the Session registry, durable command submission, Session ↔ Sandbox routing           | Sandboxes, Events, stored history                                   |
| History Service | ingress, raw event log, delta settlement, thread fold and projection, read API, scoped history reads, the index of which Sessions exist | Sandboxes, commands                                                 |
| App             | browser auth and API, app-only Thread metadata                                                                                          | folds, raw history, runner routing, which Sandbox a Session runs in |

Properties the target keeps:

- **Ingress is pluggable per deployment, control is not:** the Sandbox Service's configuration
  names the one ingress every Sandbox it creates pushes to; the control endpoint is always the Runner Gateway, so lifecycle requests such as
  `Seal` always have a path. A historyless client implements the ingress itself and sends commands
  through the gateway. The History Service knows Sessions from what it is pushed, not Sandboxes, and
  binds each Session to the workload identity of its first writer.
- **One History Service** (operator, 2026-10-11 PDT): one binary and Deployment takes what runners
  push and serves reads from the same database. The ingress keeps a listener of its own, so the
  network fence admits Sandboxes to it and nothing else.
- **History Service outage:** runners keep running and commands keep flowing. The runner journal
  is the buffer, so an outage is unacknowledged lag; teardown waits until the ingress has
  acknowledged through every seal cursor.
- **Sessions can outlive a Sandbox later:** the History Service keys by Session and records each
  Sandbox as an incarnation, and a teardown seal ends an incarnation, not the Session, so reviving a
  Session in a new Sandbox ([task DAG section 8](task_dag.md#8-sessions-that-outlive-their-sandbox))
  needs no change here.

Invariants once the refactor is done (operator, 2026-10-11 PDT). Each one still broken is closed by
a node in the [task DAG](task_dag.md):

- **One provisioner.** Only the Sandbox Service creates, changes or deletes Sandboxes, whoever asks
  for them, the Action Service's command Sandboxes included.
- **One durable event log outside the runner:** the History Service's, written only through its
  ingress. The Runner Gateway's command submission records keep each admission entry; they are
  command receipts, not a second log.
- **One Thread projection:** the History Service fold. No other service derives Thread state from
  Session events, and Electric serves only that fold.
- **No old history in the app:** no raw Events and no fold tables in the `app` database.
- **One database per service**, named for it, with its own role. No service reads another's
  database; Electric's logical replication of the history fold is the one exception.
- **The Runner Gateway routes and does not provision.** It holds the control stream, Session ↔
  Sandbox routing and command submissions, and creates no Kubernetes objects.
- **The History Service knows Sessions and their writers, never Sandboxes.**
- **One Session ID everywhere**, minted by the client: runner, gateway, History Service and app
  use the same UUID.
- **Runners only dial out.** No runner listens for connections.
- **Derived data is rebuildable:** any fold table can be dropped and rebuilt from the raw log.
- **The app is a facade.** It stores browser sessions, operator-set Thread metadata and the Thread
  → Session link, and asks the gateway where a Session runs.
- **Coordination lives in Kubernetes** (Leases, Services), except a fence that must commit in the
  same transaction as the data it guards (the fold's `session_projection_lease`).
- **No transitional leftovers:** no compatibility shims, met `CLEANUP` tombstones, unread tables or
  columns, or cutover names.

## Facts the order rests on

- **The `sandbox_service` database holds Session history** (`session_history` and
  `session_event`, `history_service/storage/db.py`; the package has its own migrations).
  Handing it to the History Service moves the raw log with no copy. The Session registry and command
  admission live in the Runner Gateway's own `sandbox_commands` database.
- **The app still keeps its own copy of the fold** (`thread_entity`, `thread_evidence`,
  `thread_native_link`, `thread_payload_chunk`/`_manifest`, `thread_checkpoint`,
  `session_projection_lease`) with its projector and Electric source, though both environments
  now read the History Service's projection through its own Electric source.
- **Projections are derived.** A rebuild into a new epoch from the raw log is supported
  ([storage and durability](../docs/thread_view_sync.md#storage-and-durability)); only the raw log is
  irreplaceable. The staging-preservation rule ([`AGENTS.md`](../AGENTS.md#preserve-staging-data-during-service-extractions))
  binds the raw log and identities, not a rebuildable projection.
- **Settlement's target** is the 4.3 GB `session_event` and the 2.2 GB of per-delta evidence rows
  (2026-10-10 sample).

## Decided (operator, 2026-10-10 PDT)

- **Fold placement:** the projector and fold (now `history_service/fold/`), the projection tables
  above with their per-Session leases, and `thread_history_summary` (every field derived from the
  log) move. Thread `name` and `archived`,
  which the operator authors, stay in the app, as does `event_log` reduced to the Thread → Session
  link.
- **Reads:** for now only the app's ServiceAccount reads history, for every Session; agent reads
  come with `THREAD_READ_POLICY`, enforced in the History Service.
- **Existing runners:** not upgraded in place. Sandboxes on images that cannot dial out are archived
  and deleted, and inbound access is retired once none is left.
- **Command submission records** stay in the Runner Gateway for now.
- **Cutover:** clean, like the dial-out cutover; Sandboxes from before it are archived the old way
  and deleted.
