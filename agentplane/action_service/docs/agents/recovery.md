# Action recovery and cancellation

Paths are relative to the Actions base URL; use the workload authentication in your platform
instructions. Keep the request ID and original idempotency key until the task is settled.

## Lost submission response

Find the original request with
`GET /v1/action-requests?idempotency_key={original_key}` (`agentplane actions get --key KEY`).
A repeated POST is refused; neither a retry POST nor a new key recovers the response. After a lost
response `agentplane actions submit` makes one such lookup itself and exits `15` when it finds
nothing; run `get --key` later and never resubmit. Admission can still be running when a lookup
initially finds nothing: use a bounded lookup of the same key and report unresolved submission
rather than issuing another operation.

Read the original request and its events to establish the decision and execution outcome. A
transport error, expired wait, or `execution_unknown` does not prove that execution failed to
start. Do not repeat work whose outcome is uncertain.

## Abandoning an Action or choosing an alternative

Cancel your own request with `POST /v1/action-requests/{id}/cancel` (no body), `agentplane actions cancel ID`,
or MCP `cancel_action_request(request_id)`. Inspect both `outcome` and the returned request receipt;
the command exits `0` for `cancelled` and `already_cancelled`, `16` for `too_late` and `17` for
`already_finished`:

| Outcome                            | What to do                                                                                                                                    |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `cancelled` or `already_cancelled` | Withdrawal is confirmed. You may use an independently authorized alternative.                                                                 |
| `too_late`                         | Dispatch was already claimed. Cancellation cannot interrupt execution; follow the original to its outcome before considering another attempt. |
| `already_finished`                 | Inspect the original decision and result; do not repeat work that already succeeded.                                                          |

After an ambiguous cancellation response, read the original request or retry cancellation for
that same ID. Never assume withdrawal. If execution remains uncertain, report it instead of
risking a duplicate.

Stopping a wait, disconnecting, ending a turn, or deleting a notification subscription does not
cancel an Action. Cancellation of an Action also does not clean up its notification subscriptions;
remove those when their purpose is complete.
