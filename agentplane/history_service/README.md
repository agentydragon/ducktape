# History Service

Serves retained Session history: the raw runner Event log and its observation metadata, keyed by
public Session ID. It authenticates callers by projected ServiceAccount token (TokenReview) and
serves only the configured reader accounts. A Session UUID by itself grants nothing.

Today the Sandbox Service still writes the history tables and runs their migrations. This service
reads them, in read-only transactions, through `sandbox_service/session_history`. Target and sequence:
[History Service plan](../plans/history_service.md).

With `fold_shadow` set, it also folds every retained Session into its own copy of the thread
projection (`agentplane.thread_fold`), in epoch `history-v1`, from cursor 0: a page per Session per
cycle under the per-Session projection leases, so replicas share the work. Those tables are the only
ones it writes. Each cycle logs how many Sessions and events the fold is behind.

Session summaries (`ListSessionSummaries`, `GetSessionSummary`) read that fold. They list newest
first by the time of a Session's first event, with not-yet-folded Sessions ahead, and report the
committed cursor beside the folded one. A page token continues only the filter it was issued for.

`ReadThreadEntities` serves a window of a Session's fold rows, by entity index or by key, each as
the `ThreadEntityView` JSON browsers sync, and `ReadPayload` the chunks of a payload revision an
entity references. A caller holding rows of a replaced epoch gets `FAILED_PRECONDITION`.
`ReadEntityEvidence` pages the observation cursors an entity was folded from, and
`ReadNativeFrames` the native harness frames linked to one of them, read from the raw log.

Bazel targets: `:main`, `:image`. Settings come from `AGENTPLANE_HISTORY_SERVICE_*` environment
variables or the YAML file named by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` (`settings.py`). gRPC
listens on `port`, and `/healthz` on `health_port` reports process liveness only.
