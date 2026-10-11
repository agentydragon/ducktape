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
    App -- commands --> SS[Sandbox Service]
    App -- thread and raw reads --> HS[History Service]
    Electric -- logical replication --> HSDB[(history database)]
    App -- auth proxy --> Electric
    Runner -- control stream: commands, receipts, lifecycle --> SS
    Runner -- pushes journal Events, acked by cursor --> HSI[History Service ingress]
    HSI --> HSDB
    HS --> HSDB
    SS --> SSDB[(sandbox_commands database: command admission)]
```

| Component       | Owns                                                                                                                                             | Does not own                       |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------- |
| Runner          | harness driving, journal, journal-ordered command admission, pushing its journal to one ingress                                                  | durable history beyond its volume  |
| Sandbox Service | provisioning, lifecycle, grants, egress, the runner control stream and its authentication, durable command submission, Session ↔ Sandbox routing | Events, stored history, folds      |
| History Service | ingress, raw event log, delta settlement, thread fold and projection, read API, scoped history reads, the index of which Sessions exist          | Sandboxes, commands                |
| App             | browser auth and API, app-only Thread metadata                                                                                                   | folds, raw history, runner routing |

Properties the target keeps:

- **Ingress is pluggable per deployment, control is not:** the Sandbox Service's configuration
  names the one ingress every Sandbox it creates pushes to; the control endpoint is always the Runner Gateway, so lifecycle requests such as
  `Seal` always have a path. A historyless client implements the ingress itself and sends commands
  through the gateway. The History Service knows Sessions from what it is pushed, not Sandboxes, and
  binds each Session to the workload identity of its first writer.
- **History Service outage:** runners keep running and commands keep flowing. The runner journal
  is the buffer, so an outage is unacknowledged lag; teardown waits until the ingress has
  acknowledged through every seal cursor.
- **Settlement beside the fold:** the log, settlement and fold evidence live in one database,
  and a settlement commits with the completion it settles, so every page carrying that completion
  carries the settlement and the fold drops the chunks' evidence when it reaches it. Readers outside
  the History Service see settled ranges only through its read API.
- **Sessions can outlive a Sandbox later:** the History Service keys by Session and records each
  Sandbox as an incarnation, and a teardown seal ends an incarnation, not the Session, so reviving a
  Session in a new Sandbox ([task DAG section 8](task_dag.md#8-sessions-that-outlive-their-sandbox))
  needs no change here.

## Facts the order rests on

- **The `sandbox_service` database holds Session history** (`session_history` and
  `session_event`, `sandbox_service/session_history/db.py`; the package has its own migrations).
  Handing it to the History Service moves the raw log with no copy. The Session registry and command
  admission live in the Sandbox Service's own `sandbox_commands` database.
- **The app still keeps its own copy of the fold** (`thread_entity`, `thread_evidence`,
  `thread_native_link`, `thread_payload_chunk`/`_manifest`, `thread_checkpoint`,
  `session_projection_lease`) with its projector and Electric source, though both environments
  now read the History Service's projection through its own Electric source.
- **Projections are derived.** A rebuild into a new epoch from the raw log is supported
  ([storage and durability](../docs/thread_view_sync.md#storage-and-durability)); only the raw log is
  irreplaceable. The staging-preservation rule ([`AGENTS.md`](../AGENTS.md#preserve-staging-data-during-the-sandbox-service-extraction))
  binds the raw log and identities, not a rebuildable projection.
- **Settlement's target** is the 4.3 GB `session_event` and the 2.2 GB of per-delta evidence rows
  (2026-10-10 sample).

## Decided (operator, 2026-10-10 PDT)

- **Store placement:** the History Service takes over the existing `sandbox_service` database and
  role; the Sandbox Service gets a new `sandbox_commands` database for command admission. A `TODO` at the database
  declaration records that the name is then a misnomer. Rejected: a new database with a one-off
  copy of `session_event`, which is a second migration like the one just finished; its one
  advantage, returning disk by dropping the old copy, `pg_repack` also gives in place.
- **No rename:** `session_event` keeps its name.
- **Fold data:** the History Service projection is rebuilt from the raw log, not copied from the
  app. It is a long migration and runs as resumable, rate-bounded background work.
- **Live updates:** Electric replicates the history database behind the app's unchanged
  authorization proxy, at least for now. A History Service subscription API stays one of the
  [thread sync](thread_sync/README.md#a-second-implementation) candidates.
- **Admission reconciliation:** the Sandbox Service settles an ambiguous submission by delivering
  the same `command_id` again over the control stream, which returns the original receipt; it
  reads no journal and waits on no subscriber
  ([runner channel](../docs/runner_channel.md#dispatch-attempts)). Positive admission evidence still
  cannot regress.
- **Fold placement:** the projector and fold (`thread_fold/`, shared by the app and the History
  Service), the projection tables above with their per-Session leases, and
  `thread_history_summary` (every field derived from the log) move. Thread `name` and `archived`,
  which the operator authors, stay in the app, as does `event_log` reduced to the Thread → Session
  link, its harness, model and cwd copies dropped. The read API keys by public Session ID and serves
  list summaries, an entity window, payloads by reference and raw/debug reads.
- **Reads:** for now only the app's ServiceAccount reads history, for every Session; agent reads
  come with `THREAD_READ_POLICY`, enforced in the History Service.
- **Existing runners:** not upgraded in place. Sandboxes on images that cannot dial out are archived
  and deleted, and inbound access is retired once none is left.

## Decided (operator, 2026-10-10 PDT)

- **Ingress and control split:** the runner dials out twice, pushing Events to its ingress and
  holding a control stream to the Sandbox Service, which stays one service for Sandbox lifecycle
  and control. Rejected: following runners through the Sandbox Service, which made it fan out a
  journal it does not keep and needed holds for durability; and merging the raw log into the
  Sandbox Service, which reverses the symmetric-client decision and couples live delivery to the
  history database.
- **One ingress per Sandbox:** fan-out, if needed, is its own service.
- **Session IDs:** the client mints a UUID when creating a Session; the History Service's index
  records which Sessions exist.
- **Command submission records** stay in the Sandbox Service for now.
- **Cutover:** clean, like the dial-out cutover; Sandboxes from before it are archived the old way
  and deleted.
- **Deployments:** the Sandbox Service and the Runner Gateway are separate services and images,
  with separate ServiceAccounts.

## APIs

The implemented APIs are `sandbox_service/protocol.proto` and `history_service/protocol.proto`
([Sandbox Service API](../sandbox_service/API.md)). Live browser updates are not an RPC: Electric
replicates the projection tables behind the app's authorization proxy.

| Caller               | Sandbox Service                      | History Service                    |
| -------------------- | ------------------------------------ | ---------------------------------- |
| App                  | sandboxes, Sessions, `SubmitCommand` | summaries, entities, payloads, raw |
| Runner               | control stream                       | ingress                            |
| Historyless client   | Sessions, `SubmitCommand`            | (implements its own ingress)       |
| Notification Service | `SubmitCommand`                      |                                    |
