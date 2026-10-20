# Actions HTTP reference

Paths are relative to the Actions base URL (`AGENTPLANE_ACTIONS_URL`, also in your platform
instructions). Send `Authorization: Bearer agentplane-credential-agentplane-workload` through the
configured egress proxy. `agentplane actions` wraps each route below; [OpenAPI](/openapi.json) has the
complete request and response schemas. The operator routes are not a workload API.

| Route                                            | Command                                         | Returns                                                                    |
| ------------------------------------------------ | ----------------------------------------------- | -------------------------------------------------------------------------- |
| `GET /v1/action-groups`                          | `actions groups`, `actions list`                | The catalog                                                                |
| `GET /v1/action-groups/{group}/actions/{action}` | `actions show GROUP/ACTION`                     | One Action's current argument schema                                       |
| `GET /v1/action-policy`                          | `actions policy`                                | Your bindings, `synced`, expiry and `auto_approve_if` argument constraints |
| `POST /v1/action-requests`                       | `actions submit`                                | The receipt of a recorded request                                          |
| `GET /v1/action-requests`                        | `actions requests`, `actions get --key KEY`     | Your requests, newest first                                                |
| `GET /v1/action-requests/{id}`                   | `actions get`, `actions wait`, `actions result` | The receipt: current decision, execution and result                        |
| `GET /v1/action-requests/{id}/events`            | `actions events ID`                             | Retained events                                                            |
| `POST /v1/action-requests/{id}/cancel`           | `actions cancel ID`                             | The cancellation `outcome` and the receipt                                 |

## Submit

`POST /v1/action-requests` takes a unique caller-chosen `idempotency_key` (at most 200 characters),
an `action` identity (`group` and `name`), `arguments` matching the discovered schema, and a
human-readable `title` with an optional `description`. A repeated key is refused rather than
replaying the original response.

## Read

- `GET /v1/action-requests/{id}`: add `wait_seconds` (a number, at most the instance's maximum; over
  it is refused) and `wait_until` (`decision` or `terminal`, the default) to hold the read until that
  condition. A deadline returns the current receipt with status 200, which is not a failure and does
  not cancel anything.
- `GET /v1/action-requests/{id}/events?after_sequence={last_sequence}`: advance the sequence only
  after recording the returned events, and page after the last sequence when needed.
- `GET /v1/action-requests`: optionally `state`; `limit` (at most 128) and, for the next page,
  `after_id` set to the last request ID you already have; `idempotency_key={key}` finds the request
  submitted under that key.

## Cancel

`POST /v1/action-requests/{id}/cancel` takes no body. The MCP equivalent is
`cancel_action_request(request_id)`. The `outcome` is `cancelled`, `already_cancelled`, `too_late`
or `already_finished`; the [recovery guide](recovery.md) says what each permits.
