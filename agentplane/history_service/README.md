# History Service

Serves retained Session history: the raw runner Event log and its observation metadata, keyed by
public Session ID. It authenticates callers by projected ServiceAccount token (TokenReview) and
serves only the configured reader accounts, today the integration app's. A Session UUID by itself
grants nothing. Consumers use `client.py`; `testing/backend.py` serves the real boundary over a test
database.

This service's `migrate` init container runs the history tables' migrations
(`storage/`); its runner ingress and its fold are their only writers. Target and sequence:
[History Service plan](../plans/history_service.md).

## Runner ingress

`ingress.py` serves `RunnerIngress.Push`, the
[runner ingress contract](../docs/runner_control.md#event-ingress): runners push each Session's
journal here, one call per Session. It runs as its own binary and image (`ingress_main.py`,
`:ingress_image`), apart from the read API, since it takes untrusted input. Its settings name the
token audience and the namespaces whose ServiceAccounts may push. Each environment deploys it as
`agentplane-history-ingress` (`cluster/cdk8s/agentplane/history_ingress.py`), reachable from a
Sandbox only through the egress proxy under the `runner-ingress` policy, which substitutes the
Pod's token for the `agentplane-runner-ingress` audience.

The first accepted announcement binds the Session to the pushing ServiceAccount (namespace, name,
UID) in `session_history`; other accounts get `PERMISSION_DENIED`. Entries commit through
`Store.append`, which accepts exact replays and refuses gaps and conflicting entries, and each
acknowledgement follows its commit.

## Delta settlement

With the ingress's `settle_deltas` set, each `Store.append` batch that completes an item drops that
item's streamed delta entries (native frames and their derived deltas) from `session_event`, in
the same transaction, when every frame matches an exact versioned template
(`agentplane/native/delta_templates.py`) and the retained completion holds exactly their
concatenation. Anything else in the span keeps every entry. The dropped cursors are recorded as a
`SettledDeltas` with its ranges, and read pages report the settlements that cover or complete in
them; the fold and the app's raw-history readers treat a settled cursor as absent. The runner
journal keeps every entry. Off by default; on in testing and staging. Why and what it drops:
[delta settlement ADR](../docs/adr_delta_settlement.md).

## Thread fold

With `fold_shadow` set, it folds every retained Session into its own copy of the thread
projection (`agentplane.thread_fold`; `thread_*` tables, since a Thread is the projection of a
Session: [layering](../docs/thread_layering.md#authorities-and-representations)), in epoch `history-v1`, from cursor 0: a page per Session per
cycle under the per-Session projection leases, so replicas share the work. The fold writes only those
tables. Cycles run back to back while any Session is behind, and committed history changes wake a
caught-up fold. `fold_interval_s` remains the retry interval after failures and while another replica
holds a Session lease. Each cycle logs how many of its leased Sessions and events it is behind. A page that carries an item's completion carries its settlement, so the fold skips the settled
cursors and drops evidence it recorded for them before they settled.

Session summaries (`ListSessionSummaries`, `GetSessionSummary`) read that fold. They list newest
first by the time of a Session's first event, with not-yet-folded Sessions ahead, and report the
committed cursor beside the folded one. A page token continues only the filter it was issued for.
`BatchGetSessionSummaries` reads up to 200 named Sessions' summaries at once, leaving out any it has
no history for.

`WatchSessionSummaries` tells a reader when to re-read: each fold commit NOTIFYs the history
database, and every replica forwards that to its watchers as a payload-free `changed`. A stream opens
with one `changed` to cover whatever the caller missed, and ends with `reconnect_required` after
`watch_lease_s`.

`WatchSessionHistory` uses the same invalidation pattern for committed raw history and feed-state
changes. The app uses it to wake its local projection coordinator; it does not connect to the history
database. The fold subscribes to the same PostgreSQL notifications in-process.

`ReadThreadEntities` serves a window of a Session's fold rows, by entity index or by key, each as
the `ThreadEntityView` JSON browsers sync. A caller holding rows of a replaced epoch gets
`FAILED_PRECONDITION`.
`ReadEntityEvidence` pages the observation cursors an entity was folded from, and
`ReadNativeFrames` the native harness frames linked to one of them, read from the raw log.

Bazel targets: `:main`, `:image`. Settings come from `AGENTPLANE_HISTORY_SERVICE_*` environment
variables or the YAML file named by `AGENTPLANE_HISTORY_SERVICE_CONFIG_FILE` (`settings.py`). gRPC
listens on `port`, where `grpc.health.v1.Health` reports process liveness only.
