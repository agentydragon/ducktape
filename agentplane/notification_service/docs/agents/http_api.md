# Notifications HTTP reference

Paths are relative to the Notifications base URL (`AGENTPLANE_NOTIFICATIONS_URL`, also in your platform
instructions). Send `Authorization: Bearer agentplane-credential-agentplane-notifications-workload`
through the configured egress proxy. `agentplane notifications` wraps the subscription routes below;
[OpenAPI](/openapi.json) has the complete request and response schemas and limits.

| Route                                        | Command                             | Use                                                                   |
| -------------------------------------------- | ----------------------------------- | --------------------------------------------------------------------- |
| `GET /v1/sources`                            | none                                | Enabled providers and their subscription schemas                      |
| `POST /v1/subscriptions`                     | `notifications subscribe github pr` | Create a subscription                                                 |
| `GET /v1/subscriptions`                      | `notifications subscriptions`       | List yours                                                            |
| `GET /v1/subscriptions/{id}`                 | `notifications subscription ID`     | Source failures and expiry                                            |
| `PATCH /v1/subscriptions/{id}`               | `notifications renew ID`            | Renew: send the current `version` and the desired `lifetime_days`     |
| `DELETE /v1/subscriptions/{id}`              | `notifications cancel ID`           | Cancel                                                                |
| `GET /v1/inboxes/{inbox_id}/entries`         | none                                | Retrieve entries ([inbox handling](inbox_handling.md))                |
| `PUT /v1/inboxes/{inbox_id}/acknowledgement` | none                                | Acknowledge a contiguous prefix ([inbox handling](inbox_handling.md)) |

## Create a subscription

`POST /v1/subscriptions` takes the destination, the Session and a source:

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

Use the exact `destination_ref` and `session_id` supplied to you. For an `actions` source, start
`after_sequence` at zero or the last Action event sequence you actually recorded; it is not an inbox
cursor.

Keys are local to the qualified Session inbox. Retry creation with the identical body and key;
changing the body under the same key is refused, and reusing a cancelled subscription's key returns
that cancelled record. The response carries the subscription `id` and `inbox_id`.

For a GitHub source replace `source` with:

```json
{
  "provider": "github",
  "repository": "owner/repo",
  "subject": { "kind": "pull_request", "number": 123 }
}
```

The other subjects are `{"kind":"issue","number":123}`, `{"kind":"branch","name":"devel"}` and
`{"kind":"commit","sha":"FULL_40_CHARACTER_COMMIT_SHA"}`. Omit `events` for the subject's defaults. To
narrow a pull request to comments, add `"events":[{"event":"issue_comment","actions":["created"]}]` to
the source. Use the advertised schema for valid subject and filter combinations; workflow-job
notifications require an explicit filter.
