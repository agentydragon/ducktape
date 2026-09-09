# Asynchronous approvals: remaining delivery work

## Burn-down in this PR

The implementation slice in PR #5937 now covers the NOTIFY delivery support: Action Service
PostgreSQL notification fanout, authenticated app SSE snapshots, listener recovery, durable
per-subscription push delivery/retry/recovery, subscription ownership protection, browser
registration management, and service-worker approval controls. The remaining work below is
acceptance or explicitly separate product scope; do not reopen a second approval lifecycle for it.

Human Decisions, synchronous provider aggregation, shared notes, durable cursor-based events,
notification-driven bounded waits, and owner-only pre-claim cancellation are implemented. Use the
[Action Service specification](../action_service/SPEC.md) and [README](../action_service/README.md),
not a second lifecycle contract here. Native harness approvals remain disabled under the
[runner contract](../runner/SPEC.md).

## Web push approval notification (`NOTIFY`)

The Action Service owns canonical Action event fanout and the initial background Web Push sender,
so every replica observes the same committed transitions through PostgreSQL `NOTIFY`. The
integration app owns the browser experience: its frontend owns the service worker, browser
subscription, notification click handling, Approve/Deny presentation, and a Settings surface to
register this browser and manage the operator's other registered browsers; its backend/BFF owns
subscription-management routes and proxies the authenticated Decision route. For an open Actions
page, use the app's existing SSE pattern for server-pushed snapshots/changes rather than polling.

Notify the operator that an ActionRequest needs review with only safe/redacted context. Stale or
duplicate buttons cannot overwrite a winning Decision or create a parallel human lifecycle. The Actions page's SSE stream reconnects from an authoritative snapshot boundary; it does not fall
back to a timer poll while the stream is healthy. The Web Push notification remains the background
fallback when no tab is open or the stream is unavailable. A later Event & Notification Hub may
take over delivery, but is not a prerequisite for this first slice. Prove notification retries and
review races without duplicate effects. Subscription rows and fanout must be safe across multiple
Action Service and app replicas; reconnect/replay comes from durable state, never process-local
memory.

Implementation is complete for this code slice. It is not a prerequisite for human-approved
Claude.ai acceptance, and the deployed browser/BFF verification remains `APPROVALUI` in the
[task DAG](task_dag.md). Live VAPID delivery, revocation, unavailable-push fallback, and
retry/race behavior still require deployed acceptance; CI does not claim those outcomes.

## Concrete progress and unknown-outcome observations

A concrete long-running consumer must define supported bounded, authorized progress/status reads.
Use authoritative reconciliation only where the backend supports it; otherwise an unknown outcome
remains unknown. Tie observations to the existing Execution, without another claim or replay.
Adapter acceptance belongs in
[operations and access](operations_and_access.md).

## Originating-Thread delivery (`ING`)

Consume the canonical Action event sequence for notification matching, batching, rate limits,
offline delivery, and Thread wake/ingress. Preserve individual events and ordering; do not add a
second Action outbox or event store. Cross-Identity delivery requires an explicit read policy.
Thread input queueing/replay is independent `INPUT_DELIVERY` work and requires native Claude/Codex
research and capture review before common-protocol changes.

## Provider-error log safety (`PROVIDERLOG`)

The previous plan claimed raw provider exception text was never logged. Current
`ActionService._ask` calls `logger.exception`, and the test only inspects `record.getMessage()`,
excluding traceback formatting. Prevent sensitive exception material in rendered logs and test
the complete formatted output with a sentinel secret. Durable bounded error codes and provider
aggregation behavior already exist and must remain unchanged.

## Configurable policies

[Action policies](action_policies.md) owns configurable mandatory authorization bounds and reusable
auto-approval deciders. Failure of a mandatory bound must fail closed rather than becoming a
no-opinion vote overridden by another provider. This future distinction is not a claim about the
current optional provider aggregation. The first external OAuth slice uses human approval and
does not depend on selecting policy storage or composition.

Expiry, operator-presence heuristics, LLM deciders, a policy DSL, and standing-grant issuance remain
outside this delivery slice.
