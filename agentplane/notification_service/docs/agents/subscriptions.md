# Subscribe, wait, and clean up

Commands are `agentplane notifications …`; routes and request bodies are in the
[HTTP reference](http_api.md). Check `GET /v1/sources` there for enabled providers and their
creation schemas.

## Follow an Action

Keep the Action's actual request ID and original submission idempotency key. If submission is
uncertain, recover through the Actions API first; do not invent a request ID or submit another
Action. For an already-finished Action, read its result directly.

The command does not create Action subscriptions yet: create one with `POST /v1/subscriptions` and an
`actions` source ([body](http_api.md#create-a-subscription)). Replace the destination and Session
values with the exact supplied values and the Action ID with the real returned ID. Start
`after_sequence` at zero or the last Action event sequence you actually recorded. Action history
replay covers approval/completion between submission and subscription creation.

Retry creation with the identical body and subscription key. Confirm success and retain the
returned subscription `id`, `inbox_id`, and which task depends on it. A failed creation is not a
subscription.

## Wait without polling

Subscribe immediately when useful; a synchronous wait need not time out first. Prefer subscriptions
for approval waits, extended execution, or work that can proceed in parallel. Do independent work;
do not perform dependent steps until their prerequisites succeeded. If nothing useful remains,
report what is waiting and end your turn. A notice arrives as an automated user message, starting
another turn when your harness is idle. Read [inbox handling](inbox_handling.md) on receipt.

A notice announces retained updates; check the originating service for current status and results
before proceeding. For Actions, approval is not execution success. For GitHub, an activity event is
not proof that the current head passed required checks.

## Health, expiry, and cleanup

Inspect `agentplane notifications subscription ID` (or `subscriptions` to list) for source failures
and expiry, and `inbox.delivery_error` on inbox reads. If creation or delivery fails, use a bounded
status check or explain the limitation; do not claim monitoring is active. Notifications cannot start
a stopped harness or Sandbox.

Subscriptions expire unless renewed: `agentplane notifications renew ID --lifetime-days N`
(OpenAPI has the limits). Cancel with `agentplane notifications cancel ID` when the purpose is
complete. This does not cancel an Action or erase accepted notifications. Do not retire the entire
Session inbox to clean up one task.
