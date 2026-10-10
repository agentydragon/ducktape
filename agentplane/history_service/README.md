# History Service

Serves retained Session history: the raw runner Event log and its observation metadata, keyed by
public Session ID. It authenticates callers by projected ServiceAccount token (TokenReview) and
serves only the configured reader accounts, today the integration app's. A Session UUID by itself
grants nothing. Consumers use `client.py`; `testing/backend.py` serves the real boundary over a test
database.

Today the Sandbox Service still writes the history tables and runs their migrations. This service
reads them, in read-only transactions, through `sandbox_service/session_history`. Target and sequence:
[History Service plan](../plans/history_service.md).

## Ingester

`ingester.py` copies runner journals into the same tables as an ordinary Sandbox Service client:
`WatchSessions` names each Session and its owner, and `FollowSession` follows it from the cursor
committed here, one `Store.append` transaction per batch. It records feed state as the Sandbox
Service ingester does, and a `sealed` frame as the incarnation's end. After an outage it resumes
from the committed cursor; the runner journal is the buffer.

One replica writes a Session at a time: a follow holds a PostgreSQL session advisory lock keyed by
Session ID on its own connection, which the server releases if the replica dies. Correctness does
not rest on it: `Store.append` accepts exact replays and refuses gaps and conflicting entries.
At most `ingester.concurrency` Sessions are followed per replica; a follow ends at the Sandbox
Service's planned renewal, so waiting Sessions rotate in. The feed position is kept in memory, so a
restarted replica rereads the feed and each Session resumes from its cursor. After four consecutive
feed failures the ingester dials a fresh Sandbox Service channel.

One ingester writes the tables per environment: the cdk8s `Environment.history_writer` turns this
one's `ingester.enabled` on and the Sandbox Service's `ingest_history` off, or the reverse. It places
no retention holds yet.

## Thread fold

With `fold_shadow` set, it folds every retained Session into its own copy of the thread
projection (`agentplane.thread_fold`), in epoch `history-v1`, from cursor 0: a page per Session per
cycle under the per-Session projection leases, so replicas share the work. The fold writes only those
tables. Each cycle logs how many Sessions and events the fold is behind.

Session summaries (`ListSessionSummaries`, `GetSessionSummary`) read that fold. They list newest
first by the time of a Session's first event, with not-yet-folded Sessions ahead, and report the
committed cursor beside the folded one. A page token continues only the filter it was issued for.

`WatchSessionSummaries` tells a reader when to re-read: each fold commit NOTIFYs the history
database, and every replica forwards that to its watchers as a payload-free `changed`. A stream opens
with one `changed` to cover whatever the caller missed, and ends with `reconnect_required` after
`watch_lease_s`.

`ReadThreadEntities` serves a window of a Session's fold rows, by entity index or by key, each as
the `ThreadEntityView` JSON browsers sync, and `ReadPayload` the chunks of a payload revision an
entity references. A caller holding rows of a replaced epoch gets `FAILED_PRECONDITION`.
`ReadEntityEvidence` pages the observation cursors an entity was folded from, and
`ReadNativeFrames` the native harness frames linked to one of them, read from the raw log.

Bazel targets: `:main`, `:image`. Settings come from `AGENTPLANE_HISTORY_SERVICE_*` environment
variables or the YAML file named by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` (`settings.py`). gRPC
listens on `port`, and `/healthz` on `health_port` reports process liveness only.
