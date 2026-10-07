# Sandbox Service extraction

Status: **extraction and app cutover implemented; independent service exercised by the staging notification proof.** The
standalone gRPC service owns inventory, provisioning/reconciliation, launch guidance and runner session access. The
production app calls it for lifecycle, manual egress grants, session management, commands and event following; there is
no direct-runner fallback. Deployment source transfers backend RBAC/network authority to the service and projects an
audience-specific app token. Existing app PostgreSQL archives/checkpoints and runner volumes stay in place. The
notification smoke test in [#8853](https://github.com/agentydragon/ducktape/pull/8853) verified the deployed
session-command/receipt path in staging. Extraction is no longer a pending notification prerequisite. This evidence does
not claim a backup/restore rehearsal, validation of every production environment, or closure of every historical
preservation/handoff checklist below.

This is the concrete backend boundary required by the [service dependency rule](../docs/service_boundaries.md). The
integration app must be a client; notifications must not start with an app API, app-owned table, or app-issued Thread
ticket dependency.

## Purpose and ownership

The Sandbox Service provisions/manages sandboxes and provides authorized access to their runner sessions. This is more
than a directory: reaching a session, determining its lifecycle state, deciding who may control/read it, and eventually
bringing it online are related backend responsibilities.

The notification service remains separate. It decides which source events match subscriptions and persists
payloads/inbox state; it asks the Sandbox Service to deliver a notice. Runners do not dial notifications, and
notifications does not acquire its own parallel provisioning/runner-control path.

The Sandbox Service owns:

- Sandbox provisioning and suspend/resume/removal operations as extracted from the app.
- Trusted mapping from an explicit destination to its provisioned sandbox and runner session, current endpoint,
  ServiceAccount authority, and lifecycle state.
- Authentication/authorization of callers at its API, distinct from direct runner network access.
- Forwarding commands and exposing runner admission/effect receipts and replayable event following.
- Session creation/configuration and backend-required instructions/context for extracted launch paths, so neither
  automated agents nor services require the integration app to prepare their sessions.

It does not own notification provider payloads, inbox acknowledgement/reminder policy, Action Decisions/execution,
native harness scheduling, browser sessions, or UI-only Thread projections. Product Thread annotations can remain in the
app without becoming a backend routing dependency.

## Minimum slice before notification v1

Extract the smallest coherent backend that supports:

1. **Inspect an explicit destination:** validated destination/account/session binding and current
   lifecycle/availability. Session IDs remain explicit; do not infer an app Thread from a workload.
2. **Submit a command to a running session:** use the existing immutable runner `Command` and stable ID. A response
   indicating forwarding is not admission, and admission is not harness confirmation.
3. **Read/replay/follow session events:** authorize the selected session, preserve runner origin and command
   correlation, and expose reconnect cursors scoped to the serving log.
4. **Independently establish/manage the destinations required by the slice:** move the relevant provisioning, session
   creation, prompt/context assembly, and discovery dependencies out of the app. No hidden requirement that the UI ran
   once to bootstrap the new service's state.

Notifications and the app use the same protobuf/gRPC backend contract. V1 has one nonempty `caller_accounts`
service-account allowlist for all RPCs, checked before inventory lookup or runner contact. Ordinary sandbox workloads do
not call Sandbox Service directly, even for their own SA. The notification service still authenticates its agent callers
at its own subscription boundary. Caller-supplied owner/UID/session identifiers select resources; they are not forwarded
credentials. Every destination must match the stored Sandbox owner/UID and verified current runner Pod.

Listed services have broad access initially; there is no separate manager/delivery tier or new command-level RBAC. Wake
remains an explicit future operation/policy decision, never a read side effect. Read/follow/command must not provision,
create, resume, or wake a destination.

## Transport decision

Use **one authoritative protobuf/gRPC service API**, not parallel REST and gRPC implementations. The integration app
retains its browser-facing HTTP API; agent-facing notification HTTP/MCP tools are a separate interface decision. HTTP
health probes are not a second service API.

The existing runner protocol already defines `Command`, `EventEntry`, `SessionSpec`, and follow cursors. Reuse those
messages instead of translating their payloads through JSON. Define a service-level contract: unary inventory,
lifecycle, session management, and command admission RPCs, plus server-streaming session following. Do not expose a
transparent tunnel to runner `Attach`. Every session operation names an explicit destination; the service authorizes it
and chooses the current runner internally. No caller-supplied runner URL or inferred current Thread.

The workload bearer travels in gRPC metadata and is checked using the shared transport-neutral workload-principal
resolver. TokenReview namespaces derive from the configured service-caller allowlist. Egress supports gRPC metadata
substitution and streaming; transport choice does not defer service API authentication or satisfy the separate
runner-authentication TODO.

Use native gRPC deadlines, cancellation, and status codes, but retain application semantics: command admission is not
completion; transport EOF is not session termination; a timeout does not prove a mutation failed. Following starts with
a native snapshot and preserves original event entries and cursors. Renew follows every 15 minutes using a terminal
`reconnect_required` observation and successful stream closure; use a 16-minute client safety deadline. Reauthenticate
on reconnect from the client's durable cursor, preserving the distinction between planned renewal, backend failure, and
native stream closure. The app commits buffered entries before renewal and reconnects without marking the feed ended;
sustained retry failures still warn. Authentication, initial attachment, and stalled downstream writes retain short
deadlines. Do not enable automatic mutation retries merely because a generated client supports them.

Production entrypoints, app callers, and service acceptance tests use the same gRPC API. No test-only RPCs: inspection
uses the initial `FollowSession` snapshot and cancellation; bootstrap is exercised through `OpenSession`. The app
presents its configured Kubernetes grant catalog without an unused backend catalog RPC. Provisioning and reconciliation
are always enabled in this service. Generated protobuf messages are canonical in the service/client, not mirrored by
Pydantic DTOs. Browser HTTP schemas belong to the app; Pydantic remains only where needed for settings/catalogs and
persisted Kubernetes input validation. HTTP is limited to service health probes; there is no parallel HTTP service
implementation. Transport acceptance must cover authenticated gRPC calls, cancellation/resource cleanup, replay across
lease expiry, native closure versus transport failure, and uncertain command admission, including a real remote-client
app ingestion test rather than only in-process/native test doubles.

## Command and notification flow

1. Notifications authenticates the agent and creates an SA-authorized subscription with an explicit, verified
   destination. The Action provider separately establishes authorized source access.
2. It durably stores matching notification payloads and a service-authored inbox notice with a stable command ID and
   covered inbox range.
3. It submits that command through the Sandbox Service with **no wake allowed in v1**.
4. The Sandbox Service validates the destination/access, resolves its runner internally, and forwards the command to the
   existing session without implicitly creating or resuming it.
5. Notifications follows the command's runner Events through the Sandbox Service and records admission,
   `HarnessUserMessageConfirmed`, or failure/no-op. Receipt replay handles reconnect without new IDs.
6. The agent reads payloads and explicitly advances the inbox HWM. That acknowledgement is unrelated to the command's
   admission/confirmation and stays entirely notification-service-owned.

For an unavailable destination, report unavailability and leave the notification pending under its retention policy. Do
not imply durable acceptance of an offline runner command by the Sandbox Service. A connection loss around forwarding
still requires reconciliation against the runner's journal; a relay does not remove the existing native-effect crash
window or create an exactly-once guarantee.

## Event following and archive ownership

Fan-out is a useful responsibility here: one or more backend attachments can serve multiple authorized readers,
including the UI and notification receipt tracking. Use shared ingestion/attachment machinery where useful, but preserve
the existing independent runner attachment semantics and per-reader cursors. Bound slow consumers and recheck
authorization for long-lived follows according to the selected policy.

The runner authors execution facts. A relay or archive preserves Event origin, ordering, raw native provenance, and
causal command IDs; it must not synthesize a duplicate successful execution event or make transport acknowledgement look
like harness consumption. Serving-log cursors and original source identity remain distinct when history is copied.

The archive boundary is settled:

- The Sandbox Service follows the runner's journal, durable on its state volume. It does not own an additional
  session-log archive or fall back to app PostgreSQL when the runner is unavailable.
- Log availability through this API depends on the runner being reachable and its state volume surviving. Durable data
  on a suspended sandbox's volume is not an online archive endpoint.
- Clients needing retention independent of that volume must archive the events themselves, preserving source identity
  and checkpoints. A client archive is not a new execution authority.
- The app retains its existing PostgreSQL archive, ingestion checkpoints, and browser projections as a client of service
  event following. Archive migration is not a required follow-up. Neither the Sandbox Service nor notifications may
  query app-owned tables.

Preserve the app's existing retained data during cutover. Moving its hosted product Thread model is not a prerequisite
for the session-scoped service API.

## Discovery and access implementation

Kubernetes already holds hosted sandbox/Pod inventory. Extract the narrow endpoint/account/lifetime lookup code
internally into the Sandbox Service (using shared Kubernetes helpers as appropriate), not into another directory
deployment. See [discovery and access notes](runner_discovery.md). Do not add a runner registration callback, raw
agent-supplied connection URL, or independent runner identity issuer.

V1 reuses Cilium-controlled access to the existing runner RPCs. After the extracted-path cutover, Sandbox Service is the
control-plane caller for those paths; app and notifications call its API rather than keeping a second direct runner
route. Audit intentional administrative/test clients and overlapping policies. Authenticate the Sandbox Service's
public/service API using existing workload/operator foundations; deferring runner RPC authentication does not make this
new API unauthenticated.

**TODO after v1:** proper authentication and transport security on runner connections, consistently for all legitimate
runner clients. No JWT issuer, app-issued ticket, or fine-grained runner RBAC is a v1 prerequisite. Network reachability
control must not be described as cryptographic RPC auth.

## Staging data preservation

**Migration default:** preserve existing staging data where feasible. Do not treat the current instance as disposable
for this extraction or use drop/recreate as the easy ownership cut. This requirement is specific to the Sandbox
Service/notification work, not a claim that all staging resources can retain their identities through every possible
topology change.

Before changing staging:

1. **Inventory state and owners.** Include sandbox/workspace volumes, runner session storage and native harness history,
   command journals/receipts, archived Events and cursors, Thread IDs/names/ archive state, and relevant
   identity/configuration/grant records. Identify any Action records or other service data affected by the move; do not
   reset unrelated services. Separate irreplaceable source/user-authored data from projections that can actually be
   regenerated.
2. **Define migration and rollback.** Prefer adopting existing resources and explicit schema/data migrations. Preserve
   session/Thread IDs, causal command/Event identities, checkpoints, and access associations wherever possible. If a
   resource cannot retain its UID or location, define and test the mapping rather than silently treating a replacement
   as the same destination. Record expected downtime/disruption and ask before a destructive or identity-breaking
   exception.
3. **Back up and prove restore.** Use approved infrastructure backup paths and verify recovery in an isolated
   environment. Define a consistency boundary for PostgreSQL and retained runner volumes; unrelated snapshots taken
   while writers advance are not automatically a recoverable cut. Keep credentials/private content out of logs and PR
   artifacts. Backups alone do not prove rollback.
4. **Rehearse the ownership handoff.** Arrange a controlled cutover of writers/ingesters so both old and new owners do
   not act concurrently. A one-time migration may read/copy existing app-owned state; that is not permission for a
   steady-state backend dependency on app tables or APIs. The resulting Sandbox Service must own its required data and
   recover with the app stopped.
5. **Validate before retiring old state.** Check retained histories, identities/correlations, cursors, Thread metadata,
   workspace content, and supported session resume. Preserve original Events rather than replaying completed commands,
   tool side effects, or Actions to reconstruct history. Verify migrated checkpoint/deduplication behavior,
   authorization, and app-independent recovery, not only aggregate row counts. Define how rollback handles writes made
   after cutover; do not blindly restore an old backup over new work. Retire old copies only after validation and the
   agreed rollback window.

If preserving some data proves infeasible, report exactly what would be lost, which alternatives were considered, and
the operational impact. **Do not delete/reset it without explicit operator approval.** This plan records requirements;
no backups, restore rehearsal, migration, or preservation guarantee has been performed by the documentation change.

## Production handoff checklist

This is a coordinated writer/authority handoff, not a database migration. Before merging/applying changed manifests,
arrange a maintenance window and prevent automatic reconciliation/image updates from switching only part of the system.
This includes the independent `agentplane-staging-binding-delegation-*` Kustomizations and image automation, not just
the main staging/testing stacks. The `unset` image pin and its comments do not suspend Flux; arrange and verify that
operational gate explicitly. Record the current app, runner and policy revisions for rollback.

1. Complete the staging inventory, consistent backup and isolated restore checks above. Record Sandbox names/UIDs, SAs,
   Pod/volume identities, Thread/session IDs and ingestion high-water marks. Do not recreate any of them. Existing
   Sandboxes without pending provisioning intent are adopted as-is.
2. Build/test, then merge only with the live handoff paused as above so devel CI can publish the service image and
   matching app image. The pin components carry an explicit `unset` bootstrap marker for the new service; wait for
   publication/image automation to replace it. Record real immutable tags/digests; pin **both** before applying the
   authority change. Fork PR CI does not publish images, and `unset` is not a deployable image pin. Keep staging/testing
   pin components and Flux image policies wired.
3. Drain old app replicas/reconcilers. Deploy the independent service, projected token, and matching app client;
   transfer existing grant-delegation subjects and runner network access together. There must not be competing old/new
   provisioning reconcilers. Keep the archive database and runner Pods/PVCs.
4. Verify app inventory, open/command/stream/resume, existing archived history and checkpoint replay; stop the app and
   verify backend inventory/provisioning/session access still works. Verify the app cannot dial runner RPCs directly.
   Expired follows reconnect without falsely ending native sessions.
5. Rollback restores a compatible app image **and** the previous RBAC/network authority together after stopping the new
   owner. Keep newly written archive rows and native journal entries; do not restore an old DB/volume snapshot over
   post-cutover work or replay commands to reconstruct history.

The source includes acceptance tests, but they do not replace this staging rehearsal. No live resources have been
mutated by the implementation work. First image publication and coordinated immutable pinning remain release
prerequisites. The bootstrap `unset` pin is deliberately not a claim that an image exists.

## Extraction sequence

1. Identify the minimal ownership cut in app inventory/provisioning, session/command bridge, prompt construction, and
   event following. Keep app archive ownership and preserve its existing checkpoints. Inventory state and validate a
   data-preserving staging migration/rollback plan before changing staging.
2. Extract the Sandbox Service with independent configuration, persistence where needed, and API authorization. No
   imports of app implementation, app-table reads, or app process/bootstrap dependency.
3. Migrate the app to consume the extracted APIs for those paths. Avoid competing provisioning/control authorities or a
   permanent direct-runner bypass of the service's authorization boundary.
4. Implement notification v1 against Sandbox Service and Action Service. Keep no-wake behavior, persisted payloads,
   explicit inbox HWM, no reminders, and existing runner evidence semantics.
5. Prove the extracted paths and notification flow with the app stopped, including backend restart, destination setup
   without UI bootstrap, replay/recovery, authorization denial, and preservation of the pre-existing staging state
   identified in the migration inventory.

The minimum extraction gates notification v1. Further provisioning/UI refactors can be staged by operation, but a newly
extracted backend operation may never depend on an app-owned fallback.

## Later, not implied by extraction

- Notification-triggered wake: separately authorize requesting sandbox/harness resume and budget it, then deliver
  through the same session interface once ready. Notifications does not become a lifecycle manager; a suspended local
  process is not expected to wake itself.
- Durable admission of commands for offline destinations: an explicit product/queue decision, not a consequence of
  adding a resume API. Keep runner journal ownership and successor-session replay honest.
- Proper runner authentication/transport security, external runners, cross-cluster routing, and product Thread
  continuity across successor sessions. None warrants a v1 reverse dependency on the app.
