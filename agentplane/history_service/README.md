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
restarted replica rereads the feed and each Session resumes from its cursor.

One ingester writes the tables per environment: the cdk8s `Environment.history_writer` turns this
one's `ingester.enabled` on and the Sandbox Service's `ingest_history` off, or the reverse. It places
no retention holds yet.

Bazel targets: `:main`, `:image`. Settings come from `AGENTPLANE_HISTORY_SERVICE_*` environment
variables or the YAML file named by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` (`settings.py`). gRPC
listens on `port`, and `/healthz` on `health_port` reports process liveness only.
