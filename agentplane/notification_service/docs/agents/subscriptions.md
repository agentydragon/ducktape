# Subscribe, wait, and clean up

Commands are `agentplane notifications …`; routes and request bodies are in
[OpenAPI](/openapi.json).

## Follow an Action

Keep the Action's actual request ID and original submission idempotency key. If submission is
uncertain, recover through the Actions API first; do not invent a request ID or submit another
Action. For an already-finished Action, read its result directly.

The command does not create Action subscriptions yet: create one with `POST /v1/subscriptions` and an
`actions` source ([request schema](/openapi.json)):

```json
{
  "destinationRef": { "namespace": "YOUR_NAMESPACE", "name": "YOUR_SANDBOX", "uid": "YOUR_SANDBOX_UID" },
  "sessionId": "YOUR_SESSION_ID",
  "idempotencyKey": "follow-YOUR_ACTION_REQUEST_ID",
  "source": { "actions": { "requestId": "YOUR_ACTION_REQUEST_ID", "afterSequence": 0 } }
}
```

Replace the destination and Session values with the exact supplied values (`AGENTPLANE_DESTINATION`
holds them as `destination_ref` and `session_id`) and the Action ID with the real returned ID. Start
`afterSequence` at zero or the last Action event sequence you actually recorded; it is not an inbox
cursor. Action history replay covers approval/completion between submission and subscription creation.

Retry creation with the identical body and subscription key. Keys are local to the Session's inbox:
changing the body under the same key is refused with `ALREADY_EXISTS`, and reusing a cancelled
subscription's key returns that cancelled record. Confirm success and retain the returned
subscription `id`, `inboxId`, and which task depends on it. A failed creation is not a subscription.

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
and expiry: the text line shows `expires TIME` and `error KIND` while a failure persists, and `--json`
has `errorKind`, `error`, `errorSince` and `retryAt`. For a GitHub pull request, `github.subject.retryAt`
shows the next mergeability refresh while the result is pending, without a failure or an inbox notice.
On inbox reads, `inbox.deliveryError` says why a
notice could not be delivered and `notice` how far the latest one got. If creation or delivery fails,
use a bounded status check or explain the limitation; do not claim monitoring is active.
A notice to a suspended Sandbox waits until the Sandbox runs again unless the Sandbox has wake on message turned on.

Subscriptions expire unless renewed: `agentplane notifications renew ID --lifetime-days N`
(OpenAPI has the limits). Exit `4` means the subscription was cancelled or changed since `renew` read
it: read it again. Cancel with `agentplane notifications cancel ID` when the purpose is
complete. This does not cancel an Action or erase accepted notifications. Do not retire the entire
Session inbox to clean up one task.
