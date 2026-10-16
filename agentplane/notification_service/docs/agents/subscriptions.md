# Subscribe, wait, and clean up

Paths are relative to the Notifications base URL from your platform instructions. Use its
notification-workload Bearer placeholder through the configured proxy. Check `GET /v1/sources`
for enabled providers and their creation schemas.

## Follow an Action

Keep the Action's actual request ID and original submission idempotency key. If submission is
uncertain, recover through the Actions API first; do not invent a request ID or submit another
Action. For an already-finished Action, read its result directly.

Create the subscription with `POST /v1/subscriptions`:

```json
{
  "destination_ref": { "namespace": "YOUR_NAMESPACE", "name": "YOUR_SANDBOX", "uid": "YOUR_SANDBOX_UID" },
  "session_id": "YOUR_SESSION_ID",
  "idempotency_key": "follow-YOUR_ACTION_REQUEST_ID",
  "source": {
    "provider": "actions",
    "request_id": "YOUR_ACTION_REQUEST_ID",
    "after_sequence": 0
  }
}
```

Replace the destination and Session values with the exact supplied values and the Action ID with
the real returned ID. Start `after_sequence` at zero or the last Action event sequence you actually
recorded; it is not an inbox cursor. Action history replay covers approval/completion between
submission and subscription creation.

Retry creation with the identical body and subscription key. Confirm success and retain the
returned subscription `id`, `inbox_id`, and which task depends on it. A failed creation is not a
subscription. Keys are local to the qualified Session inbox; changing the body under the same key
is refused. Reusing a cancelled subscription's key returns that cancelled record.

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

Inspect `GET /v1/subscriptions/{id}` for source failures and expiry, and `inbox.delivery_error` on
inbox reads. If creation or delivery fails, use a bounded status check or explain the limitation;
do not claim monitoring is active. Notifications cannot start a stopped harness or Sandbox.

Subscriptions expire unless renewed. Use `PATCH /v1/subscriptions/{id}` with its current `version`
and the desired `lifetime_days`; consult OpenAPI for limits. Cancel with
`DELETE /v1/subscriptions/{id}` when the purpose is complete. This does not cancel an Action or
erase accepted notifications. Do not retire the entire Session inbox to clean up one task.
