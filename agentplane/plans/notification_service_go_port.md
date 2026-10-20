# Notification Service Go port

Status: **proposed; nothing is dispatched.** `NOTIFICATION_SERVICE_GO_PORT` in the [task DAG](task_dag.md)
is a Candidate, and the operator ruled (2026-10-19 PDT) that the possible `NOTIFICATION_SERVICE_SPLIT` does
not hold it. Read at `devel` 3b8759ea5d. The plan burns down: a stage leaves when its work lands, and the
file goes with the Python retirement.

The port moves the service as it is: the same responsibilities, the same PostgreSQL database and schema, one
Go binary. Out of scope: the split or rename, peer messaging ([multiagent communication](multiagent_communication.md)),
a history-free notice outcome, any schema change, and the Action Service port.

## What moves

| Python today                                                              | Lines | Becomes                                                                        |
| ------------------------------------------------------------------------- | ----: | ------------------------------------------------------------------------------ |
| `store.py` (inboxes, subscriptions, cursors, claims, receipts, cleanup)   |  1263 | `store` package, hand-written SQL on `lib/pq`                                  |
| `service.py` (claim loop, source reads, notice delivery)                  |   352 | `worker` package                                                               |
| `api.py` (15 routes, two SSE streams)                                     |   413 | proto + generated facade; the webhook alone stays hand-written HTTP            |
| `models.py` (agent-facing models)                                         |   226 | proto messages                                                                 |
| `db.py` (ORM rows)                                                        |   276 | schema unchanged; Goose baseline over Alembic head `0010`                      |
| `sources/github/` (client, models, state, source)                         |  1131 | `github` package                                                               |
| `sources/actions.py`, `dispatcher_client.py`, `channels.py`, `main.py`    |   191 | Actions HTTP client, generated Dispatcher stubs, shared wakeups hub, `main.go` |
| `settings.py`                                                             |   156 | `config.go` (Viper, file plus environment, no flags)                           |
| Python tests (store, service, API, wakeups, settings, acceptance, GitHub) | ~3500 | Go tests on real PostgreSQL in the stage that ports their subject              |

## What the Go service calls

| Dependency                                        | Go side                                                                                                                                        |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Own PostgreSQL database, `NOTIFY` wakeups         | `lib/pq`, Goose. The LISTEN hub is `postgres/go/wakeups`, shared with the History Service.                                                     |
| Caller identity (TokenReview)                     | `workload_auth/go/auth`                                                                                                                        |
| Sandbox by name: UID and owning ServiceAccount    | `sandboxes/sandboxreader`, shared with the Runner Gateway: `Get` reads by name, `Incarnation` holds a destination to the UID it names.         |
| The Sandbox runner's Sessions                     | `sandboxes/runnerclient`, dialing `{sandbox}-control.{namespace}.svc:8080` per call as the Python client does                                  |
| History Service reads, Dispatcher `SubmitInput`   | Generated Go stubs of the two protos                                                                                                           |
| Action Service: request and its events, two reads | A small HTTP client. Transitional: `ACTION_SERVICE_GO_PORT` replaces it with its gRPC client.                                                  |
| GitHub REST (App JWT, installation tokens)        | `google/go-github` and `bradleyfalzon/ghinstallation` (operator, 2026-10-20 PDT). `third_party/go.mod` carries no GitHub or JWT library today. |

## Contract

`notification_service/notifications.proto` (`Notifications`, for workloads) and `notifications_operator.proto`
(`NotificationsOperator`, for the integration app alone) hold the RPCs, shapes and error codes. They are two files
so that the OpenAPI document `proto_openapi` renders from the first, one proto source per document, describes
only what an agent may call. What the servicers and callers still owe it:

- The rendered document is served at `/openapi.json`, linked from `/llms.txt`, and replaces
  `docs/agents/http_api.md` (stage H).
- The GitHub webhook stays hand-written HTTP on the facade's listener, unchanged: signed body, bounded, `202`.
  `/healthz` and `/readyz` become `grpc.health.v1` and the probes native `grpc` probes (H); the app's probe moves
  in I.
- A GitHub rate limit carries `google.rpc.RetryInfo`. The CLI reads `retry_after_seconds` from the `Retry-After`
  header today, and the stock gateway sets none, so it reads the error body's details instead (I).
- Both watches send a `snapshot` when its state changes and read again at the next debounce or expiry deadline,
  and end with `reconnect_required` after a lease, as History's watch does. The app still ends a relayed stream
  when the operator session does (H, I).

## Stored data the port must read and write exactly

"Schema unchanged" is not "stored data unchanged". Rows hold values that Python computed or serialized and the
Go service must reproduce or read back:

- `inbox.destination_key` is `sha256` of Python's `json.dumps([destination_ref, session_id], sort_keys=True)`
  and sits under a unique constraint. A different serialization makes every existing inbox look new.
- `subscription.creation` and `entry.event` are Pydantic's JSON, with the `provider` discriminator, and the
  schema's check constraints read inside them (`creation ? 'source'`, `creation #>> '{source,provider}'`).
  Idempotent creation compares them, with event and action filters as canonical sets. The store keeps its own
  structs for the stored form; they are not the proto messages.
- `github_delivery.digest` is `sha256(event NUL raw body)` and `payload` the parsed body; the subject and
  revision keys (`subject_key`) index the matching.
- `notice.runner_entry` is a serialized `EventEntry`.

The first Go stage therefore generates golden vectors from the Python code (the History port's method) and
asserts the Go values against them, before any Go code writes to a shared database.

## Stages

Each stage is a small PR or a few, in dependency order. The service keeps serving from Python until the cutover.

| Stage | Change                                                                                                                                                                                                                                                                                                                 |
| ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D     | `store`: inboxes, subscriptions, quotas, cursors, read, acknowledge, retire, cleanup, with the golden vectors and the tests of `test_store.py` and `test_wakeups.py`.                                                                                                                                                  |
| E     | `worker`: claim, fenced release, debounce, the notice state machine (persist, checkpoint, attempt, `SubmitInput`, receipts from History), two-lookup stale retirement. Ported in the order of `Service.step` and with `test_service.py`.                                                                               |
| F     | Actions source: the HTTP client, the owner check, event paging, `record`.                                                                                                                                                                                                                                              |
| G     | GitHub source in three PRs: G1 client, settings and the GitHub row in `STYLE.md`'s Go library table; G2 shared refresh state, webhook ingest and correlation; G3 subscription context and reconcile. `sources/github/test_source.py` (1577 lines) is the specification.                                                |
| H     | Servicers, the facade, the webhook handler (body cap `413`, concurrency slots `503`, ten-second bound, HMAC, `202`), `main.go`, `config.go`, health, the image target, `docs/agents/`, `/openapi.json`. Acceptance tests move here.                                                                                    |
| I     | Callers, prepared against the proto and merged with the cutover: the CLI's `notifications` group decodes into the generated Go messages; the app's two stream routes and `_notifications_proxy`; the frontend's two stream consumers; app tests and `test_visual_notifications.py`, which import the service's models. |
| J     | Testing cutover: repoint `devinfra/ci/image_targets.json`, soak the migrator, roll the image with its callers, update the README system overview and `docs/system_overview.md`.                                                                                                                                        |
| K     | Staging cutover, below.                                                                                                                                                                                                                                                                                                |
| L     | Python retirement: delete the Python service, tests, Alembic directory, migration image and CI roster entries. `settings.py` stays as the manifests' contract until `SERVICE_SETTINGS_RETHINK`, as History's does.                                                                                                     |

The CLI keeps the HTTP facade as its transport, since agents reach services through the egress proxy and I have
not checked that it carries gRPC; the generated messages still replace its hand-written structs.

## Cutover

- **Testing, then staging**, with staging's image pins held while testing soaks, as for History.
- **Backup and restore first** ([staging data preservation](../docs/staging_data_preservation.md)). On the restored
  copy, the Go service's list and read answers for every inbox and subscription equal the Python service's, field
  for field. No staging database is dropped or recreated.
- **Image before config.** The Go image accepts the current manifests' settings; the probe and port changes follow
  the pin.
- **No overlap of Python and Go workers.** The roll uses `Recreate`. GitHub does not retry a failed delivery, so
  the gap can lose webhooks; the redelivery procedure in the [API doc](../notification_service/docs/api.md) covers
  it, and staging is the only environment with the webhook route.
- **Live Sandboxes.** A Sandbox started before the cutover has the old CLI and guides, which cannot talk to the new
  shapes until it restarts. That is the "decision card before PRs breaking live Sandboxes" case: ask at stage I,
  with the number of live Sandboxes in hand.

## Risks

- **Delivery equivalence.** The notice state machine decides when a command is sent and never skipped. Nothing
  diffs two implementations of it, so the evidence is the ported tests, ordering that follows `Service.step`, and a
  real Session subscribed and notified on testing before staging.
- **Struct numbers.** `Struct` holds numbers as doubles. Check the largest integer in retained staging payloads
  before relying on it; the fallback is the payload as a JSON string field.
- **Library fit.** The GitHub client must keep three behaviors: the suspended installation, the rate-limit retry
  seconds from `Retry-After` and the reset header, and typed errors the worker classifies. G1 shows each against
  the libraries before G2 starts; a gap there is reported before a hand-written fallback is written.
- **Probes.** The app probes the service's `/healthz`; `readyz` also requires live workers, the LISTEN connection
  and a query. The gRPC health service carries both, and the app's probe moves to it in stage I.

## Defaults taken

Change any of these by saying so; none holds a stage.

- Fixes to the Python service land in Python until stage D starts. After that, a fix waits for the Go store or is
  ported with it.
- New Notification features (peer sends) land in Go, after the cutover.
- The browser keeps typed routes for the two streams, since the app resolves the Sandbox UID and maps Threads to
  Sessions before it calls the service. The generic upstream proxy does not fit them.
