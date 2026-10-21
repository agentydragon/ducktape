# Agent CLI

`agentplane` ([`cli/`](../cli/)) is a Go command an agent runs from its shell to call the HTTP APIs it
otherwise calls with `curl` through the egress proxy: the Action Service, the Notification Service and
the Egress rules API. It adds no authority, no state and no backend path. What it takes off each agent
is the part `curl` leaves to every model turn: JSON quoting, the idempotency and recovery protocol, long
waits, telling an egress refusal from a service refusal, and exit status. `agentplane --help` is what an
agent reads first; each command's `--help` says what it wraps and the exit codes that matter.

The CLI overlaps `curl` and the Action Service's MCP tools on purpose. All three reach one backend
authority (the Sandbox's ServiceAccount, through the proxy) and differ in what the caller has to write.
The HTTP routes, the served `/llms.txt` guides and `/openapi.json` stay the reference; MCP serves clients
that only speak MCP ([principles](principles.md#agent-facing-affordances-are-scriptable-apis)); the CLI
serves an agent scripting from its shell. None owns state the others lack. It is the CLI of the
[approved transport direction](runner_delivery_and_harness_design.md#api-transports-and-agent-workflows):
the facades keep these commands' contract, and a service that moves behind one updates the CLI atomically.

## Commands

One command group per service, each configured and failing independently: `agentplane actions` needs
only the Action Service, `notifications` only the Notification Service, `egress` only the Egress rules
API. A group whose configuration is absent exits `2` naming the missing variables and leaves the others
usable. Output, configuration, errors and exit codes are shared by every group, so another service
joins as one more group under them.

| Command                                                                                                                                        | Request                                                |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `actions groups`                                                                                                                               | `GET /v1/action-groups`                                |
| `actions list [GROUP]`                                                                                                                         | the same, one Action per line                          |
| `actions show GROUP/ACTION`                                                                                                                    | `GET /v1/action-groups/{group}/actions/{action}`       |
| `actions policy`                                                                                                                               | `GET /v1/action-policy`                                |
| `actions submit GROUP/ACTION --title TEXT [--description TEXT] [--arguments JSON\|@FILE\|-] [--key KEY] [--wait [--until decision\|terminal]]` | `POST /v1/action-requests`, then `wait`                |
| `actions get {ID \| --key KEY}`                                                                                                                | `GET /v1/action-requests/{id}` or `?idempotency_key=`  |
| `actions result {ID \| --key KEY}`                                                                                                             | `wait` for the terminal state, then the stored result  |
| `actions wait {ID \| --key KEY} [--until decision\|terminal]`                                                                                  | `GET /v1/action-requests/{id}` with a server-side wait |
| `actions requests [--state STATE]...`                                                                                                          | `GET /v1/action-requests`, paged                       |
| `actions events ID [--after SEQUENCE]`                                                                                                         | `GET /v1/action-requests/{id}/events`, paged           |
| `actions cancel ID`                                                                                                                            | `POST /v1/action-requests/{id}/cancel`                 |
| `notifications subscribe github pr OWNER/REPO#NUMBER [--event NAME[:ACTION[,ACTION]]]... [--lifetime-days N] [--key KEY] [--destination JSON]` | `POST /v1/subscriptions`                               |
| `notifications subscriptions`                                                                                                                  | `GET /v1/subscriptions`, paged                         |
| `notifications subscription ID`                                                                                                                | `GET /v1/subscriptions/{id}`                           |
| `notifications renew ID [--lifetime-days N]`                                                                                                   | `GET`, then `PATCH` with the read `version`            |
| `notifications cancel ID`                                                                                                                      | `DELETE /v1/subscriptions/{id}`                        |
| `notifications send NAMESPACE/SANDBOX SESSION_ID {--body TEXT \| --body-file FILE\|-} [--uid UID] [--from-session ID] [--key KEY]`             | `POST /v1/messages`                                    |
| `notifications message ID`                                                                                                                     | `GET /v1/messages/{id}`                                |
| `egress rules`                                                                                                                                 | `GET /v1/rules`                                        |

Every command takes `--json` and `--timeout`; the paged lists take `--limit` and `--after`.

Left out on purpose:

- **Inbox read and acknowledgement.** Acknowledging advances a contiguous prefix across every
  subscription in the inbox, so it stays an explicit `curl` step until a review of its own. The other
  `github` subjects (`issue`, `branch`, `commit`) would take `subscribe` the same way; only `pr` exists.
- **Following an Action by subscription,** and operator and cluster-internal routes. The CLI never calls
  `/v1/operator/...` or the egress `/decisions` read side.
- **Selecting rules by host, method or path.** `egress rules` prints the caller's view and `jq` selects
  from it; the matching semantics belong to the proxy, so a CLI-side matcher would be a second copy to
  drift.
- **Client-side argument validation.** `submit` sends `--arguments` as given (it must be JSON); the
  service validates against the current schema, so there is one validator to drift. Argument values are
  JSON because the Action's schema is JSON; there is no `key=value` form whose typing a caller would
  have to guess.

## Output

- **`--json`** prints the service's own objects, compacted but otherwise unmodified: no envelope, no
  renamed or added fields. A unary command prints one JSON value. A list prints one value per line, in
  the service's order. `jq` reads either form, and `jq -s` collects the lines. **Deviation:** a
  gRPC-Gateway service wraps each answer in a named field (`{"subscription": {...}}`,
  `{"subscriptions": [...]}`); the `notifications` commands print that field's value. Field names are
  the gateway's own: camelCase, with `int64` values such as `version` as strings.
- **`actions groups --json` is large:** the service returns every Action's full description and input
  schema inline. `actions list` prints the same Actions one per line.
- **Without `--json`** prints concise text, one line per item. Text is not a stable interface; a script
  uses `--json`.
- **Never TTY-dependent.** An agent's shell may or may not allocate a terminal, so output mode, color,
  paging and prompting do not depend on one. The CLI never prompts.
- **stdout carries the answer, stderr the diagnostics.** After an error (exit `1`-`5` or `15`) stdout is
  empty, except for `wait`, `result` and `submit --wait` ending in exit `3` ([Waiting](#waiting)). The
  outcome exits (`10`-`14`, `16`, `17`) print what the command answered: the receipt, or the
  cancellation result.
- **`get`** prints the receipt and exits `0` whatever its state. **`submit`** without `--wait` prints
  the receipt and exits `0` once the request is recorded, whatever its state.
- **`result`** prints the stored `execution.result` when the Action succeeded: the tool's own answer
  for an MCP group (a `CallToolResult`) and the executor's JSON for a sandbox group. Text mode prints
  the text content blocks, or the value as indented JSON when there are none. For any other state it
  prints the receipt, so the reason (a Decision note, `execution.error`) is visible.
- **Timestamps** are RFC 3339 UTC, as the services emit them.

## Configuration and credentials

The CLI reads the environment of the harness and of every command it runs. There are no config files and
no discovery calls, so no command depends on a service it does not talk to.

| Variable                                                                                               | Value                                                                                   |
| ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| `AGENTPLANE_ACTIONS_URL`, `AGENTPLANE_NOTIFICATIONS_URL`, `AGENTPLANE_EGRESS_URL`                      | the service's base URL: `http` or `https`, no credentials, query or fragment            |
| `AGENTPLANE_ACTIONS_CREDENTIAL`, `AGENTPLANE_NOTIFICATIONS_CREDENTIAL`, `AGENTPLANE_EGRESS_CREDENTIAL` | the inert placeholder the egress proxy substitutes for that service                     |
| `AGENTPLANE_DESTINATION`                                                                               | `{"destination_ref": {...}, "session_id": "..."}`: this Session for `subscribe`, `send` |

`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` and `SSL_CERT_FILE` are the harness environment's existing proxy
and trust settings; the CLI uses them as `curl` does. `subscribe` also accepts `--destination JSON` (the
same object, and it wins over the variable) for a Session started without the variable. The object holds
`destination_ref` (`namespace`, `name`, `uid`) and `session_id` and nothing else; anything else is exit `2`.

Credential scope:

- The CLI holds only a placeholder. It sends it as `Authorization: Bearer <placeholder>` to the
  configured base URL, follows no redirect, never prints it, and has no flag to supply another
  credential, bypass the proxy, or skip certificate verification.
- Authority is whatever the egress proxy substitutes for the placeholder: the Sandbox's ServiceAccount,
  exactly as for `curl` ([workload authentication](workload_authentication.md)). `actions policy` names
  that account in `subject`.
- A destination and Session ID address an inbox. They are not credentials.
- Variable names avoid `TOKEN`, `KEY` and `SECRET`, which a harness that filters credential-looking
  names might drop.

## Errors and exit codes

Every failure is classified once, from the HTTP status, two response headers and, for a gRPC-Gateway
service, the body. The central proxy sets `x-agentplane-egress` (`denied; reason=...`) and the Pod
sidecar sets `x-agentplane-egress-sidecar` (`reason=...`) on its refusals
([egress spec](../egress/SPEC.md)). A response carrying either is the platform's, not the service's: a
`403` without the header is the service denying, a `403` with it is the proxy denying. The error names a
gRPC status code, so the vocabulary survives the facades (gRPC-Gateway maps the same codes to HTTP
statuses). It is one line of JSON on stderr under `--json`:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "action request not found",
    "source": "service",
    "http_status": 404,
    "reason": null,
    "retry_after_seconds": null
  }
}
```

`source` is `service`, `egress`, `sidecar` or `client` (the CLI itself: usage, configuration, a lost
connection). `reason` is the `reason=` token of a refusal header when there is one. `egress` is the proxy
refusing a call from any group, not the Egress rules API, whose own refusals are `service`. In text mode
the same fields print as one line. `retry_after_seconds` is the `google.rpc.RetryInfo` delay of a gateway
body, else the `Retry-After` header. The mapping:

| HTTP                                              | gRPC code                           | Exit |
| ------------------------------------------------- | ----------------------------------- | ---- |
| `400`, `422`                                      | `INVALID_ARGUMENT`                  | `1`  |
| `401`                                             | `UNAUTHENTICATED`                   | `1`  |
| `403` (service or egress)                         | `PERMISSION_DENIED`                 | `1`  |
| `404`                                             | `NOT_FOUND`                         | `5`  |
| `409`                                             | `ALREADY_EXISTS` / `ABORTED`        | `4`  |
| `429`                                             | `RESOURCE_EXHAUSTED`                | `3`  |
| `502`, `503`, `504`, connection failure, deadline | `UNAVAILABLE` / `DEADLINE_EXCEEDED` | `3`  |
| any other status                                  | `INTERNAL` (`500`) / `UNKNOWN`      | `1`  |

A `409` is `ALREADY_EXISTS` for `submit` and `send` (a repeated key) and `ABORTED` elsewhere (a stale
`version`).

**Deviation:** a gRPC-Gateway service answers an error with a `google.rpc.Status` body, whose numeric
`code` names the gRPC code and replaces the guess above, because the HTTP status only approximates it
(`FAILED_PRECONDITION` and `OUT_OF_RANGE` arrive as `400`, `ALREADY_EXISTS` and `ABORTED` as `409`).
`NOT_FOUND` exits `5`; `ALREADY_EXISTS` and `ABORTED` `4`; `RESOURCE_EXHAUSTED`, `UNAVAILABLE` and
`DEADLINE_EXCEEDED` `3`; every other code `1`. Today that is the `notifications` group. A refusal
carrying a platform header keeps its own source and code whatever the body says.

Exit codes say what the caller should do next. `0` and `128 + signal` follow shell convention; no code
is `126` or `127`.

**Exit status is the platform's answer, never a payload's.** It reports whether the platform did what was
asked, from the service's own state (for an Action, its `state`). The CLI never reads what a service
passes through to decide it: an Action's `execution.result`, a notification's payload, the contents of an
egress rule. A sandbox script that exits `3` is a `succeeded` Action whose `execution.result.exit` holds
the `3`, so `submit --wait` exits `0`; an MCP tool's `isError: true` is the same. A number the CLI
returns is never a script's, even when they coincide. A caller that needs the payload's verdict reads it,
and the CLI has no wiring specific to one kind of Action:

```bash
agentplane actions submit GROUP/ACTION --title TEXT --arguments @args.json --wait --json |
  jq -e '.execution.result.exit.exit_code == 0'
```

Propagating a payload verdict would be a new code after `17`, without renumbering.

Codes below `11` are shared by every group and never change meaning:

| Exit | Meaning                                                                                                  | Next step                                     |
| ---- | -------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `0`  | Success. For `wait`, `result` and `submit --wait`: the awaited condition holds and the Action succeeded. | Proceed.                                      |
| `1`  | Refused or invalid, or an unclassified CLI error.                                                        | Report; do not retry unchanged.               |
| `2`  | Usage or configuration error, found before any request.                                                  | Fix the command or environment.               |
| `3`  | Transient: unreachable, deadline, rate limit, draining.                                                  | Retry a read; see `submit` below for a write. |
| `4`  | Conflict: a repeated idempotency key, a stale subscription `version`, or a cancelled key.                | Read the existing object.                     |
| `5`  | Not found, or not owned by this account.                                                                 |                                               |
| `10` | Deadline reached before the condition. The current receipt is on stdout.                                 | Keep waiting, or subscribe.                   |

A group defines outcome codes only for answers the caller must branch on, from `11` up. They are
allocated once and never reused, so a script's `case` on an exit status means the same thing whichever
group it called. `notifications` and `egress` define none. The `actions` outcomes:

| Exit | Meaning                                                                     | Next step                              |
| ---- | --------------------------------------------------------------------------- | -------------------------------------- |
| `11` | Denied.                                                                     | Report the Decision note.              |
| `12` | Failed without a result.                                                    | Report `execution.error`.              |
| `13` | Cancelled.                                                                  |                                        |
| `14` | `execution_unknown`.                                                        | Check the effects; never replay.       |
| `15` | `submit`: the response was lost and a lookup by the same key found nothing. | Run `get --key` later; never resubmit. |
| `16` | `cancel`: `too_late`; dispatch was already claimed.                         | Follow the original to its outcome.    |
| `17` | `cancel`: `already_finished`.                                               | Read the original; do not repeat work. |

`cancel` exits `0` only for `cancelled` and `already_cancelled`: exit `0` is the condition under which
the [recovery guide](../action_service/docs/agents/recovery.md) allows an independent alternative.

## Submitting

`--key` is optional. Absent, the CLI generates a random UUID and writes it to stderr before sending the
`POST` (`idempotency key: KEY`, or `{"idempotency_key": "KEY"}` under `--json`), so a killed process
leaves it in the captured stderr; the receipt on stdout carries it too. A repeated key is refused, so the
key is the only handle on a request whose response is lost: a caller retrying a failed `submit` passes
the printed key as `--key`, and a retry without it is a new request. The CLI never sends a second `POST`
for a key.

- `409` on `submit` exits `4`. The text says the key is in use and names `get --key`.
- After a failure that may have left the `POST` committed (connection lost after the request was fully
  written, a `5xx`, an answer it cannot read), the CLI makes **one** lookup by the same key, under its
  own 10 s budget because the deadline may be what lost the response. Found: it carries on as if the
  response had arrived. Not found, or the lookup fails: it exits `15`. Admission may still be running,
  so absence is not proof; the caller looks again later. The CLI has no retry timer here. A connection
  that never opened is exit `3` with nothing committed.
- `--wait` is `submit` followed by `wait` on the returned ID, with `wait`'s options and exit codes. A
  receipt that already satisfies the condition is not read again.

`notifications subscribe` derives its idempotency key from the canonical source (the events in sorted
order), so repeating the command returns the same subscription; `--key` overrides it. The service
returns a **cancelled** subscription when a cancelled key is reused, so `subscribe` exits `4` when the
response is cancelled and says to pass a new `--key`. A changed `--lifetime-days` under the same key is a
`409` (exit `4`); `renew` changes the lifetime. `--event` replaces the subject's default events rather
than adding to them, as the service does.

`notifications send` generates a random key when `--key` is absent and writes it to stderr before the
`POST`, as `submit` does. The service answers an identical retry under the key with the message it already
accepted, so a send that failed or lost its response is retried with `--key KEY` and the same text; the key
with other text is `409` (exit `4`). A message is not a subscription: identical text sent twice is two
messages, so the key is not derived from it. `--from-session` defaults to the `session_id` of
`AGENTPLANE_DESTINATION` and is omitted without it.

## Waiting

`wait` blocks until the receipt satisfies a condition. The conditions are the service's own (`WaitUntil`):
`decision` holds once the state is not `decision_pending`; `terminal` (the default) once it is
`succeeded`, `failed`, `denied`, `cancelled` or `execution_unknown`. `result` waits for `terminal`.

- The total budget is `--timeout` (seconds as `30` or `2.5`, or a duration such as `30s`), default 30 s,
  the same as the Action Service's direct wait. It applies to every command, as the deadline of the
  whole call. `--timeout 0` sets no deadline; a waiting command then reads once. An agent's shell tool
  call has its own time limit, so longer waits belong to a notification subscription
  ([subscription guide](../notification_service/docs/agents/subscriptions.md)); the CLI's job is the
  short wait.
- `wait` reads the receipt at once, so a deadline always has a receipt to print, then asks the server to
  hold the next read. Each server wait is at most 30 s (the Action Service's default
  `max_wait_seconds`) and ends half a second before the deadline; the CLI re-issues waits until the
  condition holds or the budget ends. An instance configured below 30 s refuses the wait with `422`
  (exit `1`). A receipt is current state, not an event, so every re-issue reads it afresh and no cursor
  or resume state exists.
- A failed attempt that is transient (exit `3`) backs off and retries within the remaining budget; any
  other failure ends the wait. If the budget ends while the service was unreachable the exit is `3`, with
  the last receipt read on stdout, not `10`. A receipt in a state the CLI does not know is exit `1`.
- Waiting is event-driven on the server (the committed-state invalidations of the Action Service),
  never a polling loop in the CLI.
- `wait --key` first resolves the ID with the by-key read.

## Pagination, streams and interruption

| Command                    | Server paging                                    | CLI                               |
| -------------------------- | ------------------------------------------------ | --------------------------------- |
| `groups`, `list`, `policy` | none; the catalog is bounded                     | prints all                        |
| `requests`                 | `after_id` and `limit`, at most 128 a page       | follows pages; `--after ID`       |
| `events`                   | `after_sequence` and `limit`, at most 128 a page | follows pages; `--after SEQUENCE` |
| `subscriptions`            | `afterId`, 128 a page                            | follows pages; `--after ID`       |

A list follows server pages until `--limit` items (default 50; `0` is unlimited). To resume, pass the
last printed item's `id` or `sequence` to `--after`; text mode prints that hint on stderr when the limit
cut the list. A list prints only once complete, so a failure on a later page leaves stdout empty.

No command here is a long-lived stream, and none is built on polling: `events` has no `--follow` because
the service has no push source for it. The rule for a command added over a gRPC-Gateway server stream:
stdout is the unwrapped messages as JSON Lines; a terminal error chunk becomes the error envelope and its
exit code; the cursor (`sequence`, inbox cursor) is application data, so the CLI never resumes a stream
implicitly. `wait` is the exception only because a receipt is state.

`SIGINT` and `SIGTERM` end the local command and exit `128 + signal`. They never cancel anything: a wait,
a disconnect or a killed `submit` leaves the Action and any subscription as they were. `actions cancel`
and `notifications cancel` are the two explicit operations, and neither implies the other.

## What the CLI relies on in the Action Service

- `GET /v1/action-requests/{id}?wait_seconds=<float>&wait_until=decision|terminal` returns the receipt
  after waiting event-driven up to `wait_seconds`, capped by the instance (over the cap is `422`). A
  deadline returns the current receipt with `200`.
- `GET /v1/action-requests?limit=<1..128>&after_id=<uuid>&state=...&idempotency_key=...` returns a JSON
  array, newest first. `after_id` is the id of the last request the caller already has; the next items
  follow it. An unknown or foreign `after_id` is `404`. Without `limit` it returns everything.

Both are the Action Service's own contract ([spec](../action_service/SPEC.md)).

## Rejected alternatives

- **Exit `0` whenever an answer arrives, with an opt-in failure flag** (the shape of
  `gh run watch --exit-status`). Approval is not execution success, and `execution_unknown` is not
  permission to retry, yet `agentplane actions wait ID && deploy` would proceed on a denied or unknown
  Action. Stdout carries the receipt in every case, for a script that wants the reason.
- **Polling in the CLI for `wait`, or an `events --follow` over polling.** Every polling agent adds
  load and latency the service's own invalidations already avoid; waiting is event-driven on the server.
