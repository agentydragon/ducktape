# Agent CLI: commands, JSON and exit codes

Status: **proposed, awaiting operator approval to build.** The [settled decisions](#settled-decisions)
are the operator's and no question is open. On approval the contract graduates to a doc under
`docs/` and the rollout becomes DAG nodes. This plan authorizes no CLI, API, prompt or deployment
change. It specifies the command-line contract for the transport direction approved on
2026-10-15 (gRPC internally, a gRPC-Gateway HTTP/JSON facade, and an ergonomic CLI, adopted one
service and interface at a time; see
[API transports and agent workflows](../docs/runner_delivery_and_harness_design.md#api-transports-and-agent-workflows)).

`agentplane` is a client of the HTTP APIs that agents already call with `curl` through the egress
proxy: the Action Service, the Notification Service and the Egress rules API. It adds no authority,
no state and no backend path. What it takes off each agent is the part `curl` leaves to every model
turn: JSON quoting, the idempotency and recovery protocol, long waits, telling an egress refusal
from a service refusal, and exit status.
The HTTP routes stay the reference; the served `/llms.txt` guides and `/openapi.json` keep describing
them.

The CLI overlaps `curl` and the Action Service's MCP tools on purpose. All three reach one backend
authority (the Sandbox's ServiceAccount, through the proxy); they differ in what the caller has to
write. `curl` is the reference contract, MCP serves clients that only speak MCP, and the CLI serves
an agent scripting from its shell. None owns state the others lack.

## Commands

One command group per service, each configured and failing independently: `agentplane actions`
needs only the Action Service, `agentplane notifications` only the Notification Service,
`agentplane egress` only the Egress rules API. A group whose configuration is absent exits `2`
naming the missing variable and leaves the others usable. Output, configuration, errors and exit
codes below are shared by every group, so another service joins as one more group under them.

| Command                                                                                                                                        | Request                                                                                      |
| ---------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `actions groups`                                                                                                                               | `GET /v1/action-groups`                                                                      |
| `actions list [GROUP]`                                                                                                                         | the same, one Action per line                                                                |
| `actions show GROUP/ACTION`                                                                                                                    | `GET /v1/action-groups/{group}/actions/{action}`                                             |
| `actions policy`                                                                                                                               | `GET /v1/action-policy`                                                                      |
| `actions submit GROUP/ACTION [--key KEY] --title TEXT [--description TEXT] [--arguments JSON\|@FILE\|-] [--wait [--until decision\|terminal]]` | `POST /v1/action-requests`, then `wait`                                                      |
| `actions get {ID \| --key KEY}`                                                                                                                | `GET /v1/action-requests/{id}` or `?idempotency_key=`                                        |
| `actions result {ID \| --key KEY}`                                                                                                             | as `get`, then the stored result                                                             |
| `actions wait {ID \| --key KEY} [--until decision\|terminal]`                                                                                  | `GET /v1/action-requests/{id}` with a wait ([needed](#server-operations-the-contract-needs)) |
| `actions requests [--state STATE]...`                                                                                                          | `GET /v1/action-requests` ([needs paging](#server-operations-the-contract-needs))            |
| `actions events ID [--after SEQUENCE]`                                                                                                         | `GET /v1/action-requests/{id}/events`                                                        |
| `actions cancel ID`                                                                                                                            | `POST /v1/action-requests/{id}/cancel`                                                       |
| `notifications subscribe github pr OWNER/REPO#NUMBER [--event NAME[:ACTION[,ACTION]]]... [--lifetime-days N] [--key KEY]`                      | `POST /v1/subscriptions`                                                                     |
| `notifications subscriptions`                                                                                                                  | `GET /v1/subscriptions`                                                                      |
| `notifications subscription ID`                                                                                                                | `GET /v1/subscriptions/{id}`                                                                 |
| `notifications renew ID [--lifetime-days N]`                                                                                                   | `GET`, then `PATCH` with the read `version`                                                  |
| `notifications cancel ID`                                                                                                                      | `DELETE /v1/subscriptions/{id}`                                                              |
| `egress rules`                                                                                                                                 | `GET /v1/rules`                                                                              |

Every command takes `--json` and `--timeout`; list commands take `--limit` and `--after`.

Left out on purpose:

- **Inbox read and acknowledgement.** Acknowledging advances a contiguous prefix across every
  subscription in the inbox, so it stays an explicit `curl` step until a review of its own. The
  other `github` subjects (`issue`, `branch`, `commit`) would take `subscribe` the same way; only
  `pr` is specified here.
- **Following an Action by subscription,** and operator and cluster-internal routes. The CLI never
  calls `/v1/operator/...` or the egress `/decisions` read side.
- **Selecting rules by host, method or path.** `egress rules` prints the caller's view and `jq`
  selects from it; the matching semantics belong to the proxy, so a CLI-side matcher would be a
  second copy to drift. A selector is a change to the Egress rules API.
- **Client-side argument validation.** `submit` sends `--arguments` as given; the service validates
  against the current schema, so there is one validator to drift.

Argument values are JSON because the Action's schema is JSON. There is no `key=value` form whose
typing a caller would have to guess.

## Output

- **`--json`** prints the service's own objects unmodified: no envelope, no renamed or added fields.
  A unary command prints one JSON value. A list or stream command prints one value per line, in
  the service's order. `jq` reads either form, and `jq -s` collects the lines.
- **Without `--json`** prints concise text, one line per item. Text is not a stable interface; a
  script uses `--json`.
- **Never TTY-dependent.** An agent's shell may or may not allocate a terminal, so output mode,
  color, paging and prompting do not depend on one. The CLI never prompts.
- **stdout carries the answer, stderr the diagnostics.** After an error (exit `1`-`5` or `15`)
  stdout is empty. The outcome exits (`10`-`14`, `16`, `17`) print what the command answered: the
  receipt, or the cancellation result.
- **Timestamps** are RFC 3339 UTC, as the services emit them.
- **`result`** prints the stored `execution.result` when the Action succeeded: the tool's own answer
  for an MCP group (a `CallToolResult`) and the executor's JSON for a sandbox group. Text mode
  prints the text content blocks, or the value as indented JSON when there are none. For any other
  state it prints the receipt, so the reason (a Decision note, `execution.error`) is visible.

## Configuration and credentials

The deployment already prints these values into the platform instructions; the CLI reads them from
the environment of the harness and of every command it runs. There are no config files and no
discovery calls, so no command depends on a service it does not talk to.

| Variable                                                                                               | Set by                                 | Value                                                                                                                                                                                                      |
| ------------------------------------------------------------------------------------------------------ | -------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENTPLANE_ACTIONS_URL`, `AGENTPLANE_NOTIFICATIONS_URL`, `AGENTPLANE_EGRESS_URL`                      | deployment (`--harness-env`)           | the base URLs the platform instructions list                                                                                                                                                               |
| `AGENTPLANE_ACTIONS_CREDENTIAL`, `AGENTPLANE_NOTIFICATIONS_CREDENTIAL`, `AGENTPLANE_EGRESS_CREDENTIAL` | deployment (`--harness-env`)           | the inert placeholders the instructions list per service (`agentplane-credential-agentplane-workload` for Actions and Egress, `agentplane-credential-agentplane-notifications-workload` for Notifications) |
| `AGENTPLANE_DESTINATION`                                                                               | the runner, when it launches a Session | the JSON object the instructions already end with: `{"destination_ref": {...}, "session_id": "..."}`                                                                                                       |

`HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY` and `SSL_CERT_FILE` are the harness environment's existing
proxy and trust settings; the CLI uses them as `curl` does. `notifications subscribe` also accepts
`--destination JSON` (the same object, and it wins over the variable) for a Session that was
started without the variable.

Credential scope:

- The CLI holds only a placeholder. It sends it as `Authorization: Bearer <placeholder>` to the
  configured base URL, follows no redirect, never prints it, and has no flag to supply another
  credential, bypass the proxy, or skip certificate verification.
- Authority is whatever the egress proxy substitutes for the placeholder: the Sandbox's
  ServiceAccount, exactly as for `curl` ([workload authentication](../docs/workload_authentication.md)).
  `actions policy` names that account in `subject`.
- A destination and Session ID address an inbox. They are not credentials.
- Variable names avoid `TOKEN`, `KEY` and `SECRET`, which a harness that filters credential-looking
  names might drop. Whether the pinned Claude Code and Codex pass these variables to the commands
  they run is an implementation check, not an observed fact.

## Errors and exit codes

Every failure is classified once, from the HTTP status and two response headers. The central proxy
sets `x-agentplane-egress` (`denied; reason=...`) and the Pod sidecar sets
`x-agentplane-egress-sidecar` (`reason=...`) on its refusals. A response carrying either is the
platform's, not the service's: a `403` without the header is the service denying, a `403` with it
is the proxy denying. The error names a gRPC status code, so the vocabulary survives the facade
(gRPC-Gateway maps the same codes to HTTP statuses):

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

`source` is `service`, `egress`, `sidecar` or `client`; `reason` is the egress `reason=` token when
there is one. `egress` here is the proxy refusing a call from any group, not the Egress rules API,
whose own refusals are `service`. In text mode the same fields print as one line on stderr. The mapping from HTTP:

| HTTP                                              | gRPC code                           | Exit |
| ------------------------------------------------- | ----------------------------------- | ---- |
| `400`, `422`                                      | `INVALID_ARGUMENT`                  | `1`  |
| `401`                                             | `UNAUTHENTICATED`                   | `1`  |
| `403` (service or egress)                         | `PERMISSION_DENIED`                 | `1`  |
| `404`                                             | `NOT_FOUND`                         | `5`  |
| `409`                                             | `ALREADY_EXISTS` / `ABORTED`        | `4`  |
| `429`                                             | `RESOURCE_EXHAUSTED`                | `3`  |
| `502`, `503`, `504`, connection failure, deadline | `UNAVAILABLE` / `DEADLINE_EXCEEDED` | `3`  |

Exit codes say what the caller should do next. `0` and `128 + signal` follow shell convention; no
code is `126` or `127`.

**Exit status is the platform's answer, never a payload's.** It reports whether the platform did
what was asked, from the service's own state (for an Action, its `state`). The CLI never reads
what a service passes through: an Action's `execution.result`, a notification's payload, the
contents of an egress rule. A sandbox script that exits `3` is a `succeeded` Action whose
`execution.result.exit` holds the `3`, so `submit --wait` exits `0`; an MCP tool's `isError: true`
is the same. A number the CLI returns is never a script's, even when they coincide. A caller that
needs the payload's verdict reads it, and the CLI has no wiring specific to one kind of Action:

```bash
agentplane actions submit GROUP/ACTION --title TEXT --arguments @args.json --wait --json |
  jq -e '.execution.result.exit.exit_code == 0'
```

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
allocated here once and never reused, so a script's `case` on an exit status means the same thing
whichever group it called. `notifications` and `egress` define none. The `actions` outcomes:

| Exit | Meaning                                                                     | Next step                              |
| ---- | --------------------------------------------------------------------------- | -------------------------------------- |
| `11` | Denied.                                                                     | Report the Decision note.              |
| `12` | Failed without a result.                                                    | Report `execution.error`.              |
| `13` | Cancelled.                                                                  |                                        |
| `14` | `execution_unknown`.                                                        | Check the effects; never replay.       |
| `15` | `submit`: the response was lost and a lookup by the same key found nothing. | Run `get --key` later; never resubmit. |
| `16` | `cancel`: `too_late`; dispatch was already claimed.                         | Follow the original to its outcome.    |
| `17` | `cancel`: `already_finished`.                                               | Read the original; do not repeat work. |

`cancel` exits `0` only for `cancelled` and `already_cancelled`: exit `0` is the condition under
which the [recovery guide](../action_service/docs/agents/recovery.md) allows an independent
alternative.

## Submitting

`--key` is optional. Absent, the CLI generates a random UUID and writes it to stderr before sending
the `POST` (`idempotency key: KEY`, or `{"idempotency_key": "KEY"}` under `--json`), so a killed
process leaves it in the captured stderr; the receipt on stdout carries it too. A repeated key is
refused, so the key is the only handle on a request whose response is lost: a caller retrying a
failed `submit` passes the printed key as `--key`, and a retry without it is a new request. The CLI
never sends a second `POST` for a key. The Action Service's contract: a repeated key is refused,
and the request is recovered by reading it by key.

- `409` on `submit` exits `4`. The text says the key is in use and names `get --key`.
- After a failure that may have left the first `POST` committed (connection lost after the request
  was sent, a `5xx`), the CLI makes **one** lookup by the same key. Found: it prints that receipt
  and exits as if the response had arrived. Not found: it exits `15`. Admission may still be
  running, so absence is not proof; the caller looks again later. The CLI has no retry timer here.
- `--wait` is `submit` followed by `wait` on the returned ID, with `wait`'s options and exit codes.

`notifications subscribe` derives its idempotency key from the canonical source, so repeating the
command returns the same subscription. `--key` overrides it. The service returns a **cancelled**
subscription when a cancelled key is reused, so `subscribe` exits `4` when the response is cancelled
and names `--key`. A changed `--lifetime-days` under the same key is a `409` (exit `4`); `renew`
changes the lifetime. `--event` replaces the subject's default events rather than adding to them,
as the service does.

## Waiting

`wait` blocks until the receipt satisfies a condition. The conditions are the service's own
(`WaitUntil`): `decision` holds once the state is not `decision_pending`; `terminal` (the default)
once it is `succeeded`, `failed`, `denied`, `cancelled` or `execution_unknown`.

- The total budget is `--timeout`, default 30 s, the same default as the Action Service's direct
  wait; `--timeout 0` reads once. It applies to every command, as the deadline of the whole call.
  An agent's shell tool call has its own time limit, so longer waits belong to a notification
  subscription ([subscription guide](../notification_service/docs/agents/subscriptions.md)); the
  CLI's job is the short wait.
- Each server wait is bounded by the instance's `max_wait_seconds`. The CLI re-issues waits until
  the condition holds or its budget ends. A receipt is current state, not an event, so every
  re-issue reads it afresh and no cursor or resume state exists. A dropped connection retries
  within the remaining budget; if it ends while the service was unreachable the exit is `3`, with
  the last receipt read on stdout, not `10`.
- Waiting is event-driven on the server (the committed-state invalidations of the Action Service),
  never a polling loop in the CLI.
- `wait --key` first resolves the ID with the by-key read.

## Pagination, streams and interruption

| Command                    | Server paging                        | CLI                               |
| -------------------------- | ------------------------------------ | --------------------------------- |
| `groups`, `list`, `policy` | none; the catalog is bounded         | prints all                        |
| `requests`                 | none today                           | needs the server change below     |
| `events`                   | `after_sequence`, at most 128 a page | follows pages; `--after SEQUENCE` |
| `subscriptions`            | `after_id`, 128 a page               | follows pages; `--after ID`       |

A list follows server pages until `--limit` items (default 50; `0` is unlimited). To resume, pass
the last printed item's `sequence` or `id` to `--after`; text mode prints that hint on stderr.

No command here is a long-lived stream, and none is built on polling: `events` has no `--follow`
because the service has no push source for it. The rule for a command that is added over a
gRPC-Gateway server stream: stdout is the unwrapped messages as JSON Lines; a terminal error chunk
becomes the error envelope and its exit code; the cursor (`sequence`, inbox cursor) is
application data, so the CLI never resumes a stream implicitly. `wait` is the exception only
because a receipt is state.

`SIGINT` and `SIGTERM` end the local command and exit `128 + signal`. They never cancel anything:
a wait, a disconnect or a killed `submit` leaves the Action and any subscription as they were.
`actions cancel` and `notifications cancel` are the two explicit operations, and neither implies
the other.

## Server operations the contract needs

Both are additive changes to the Action Service HTTP API and land before the commands they serve.

1. **A wait on the receipt.** Today only the MCP tools wait (`wait.wait_seconds`, `wait.wait_until`,
   capped by `max_wait_seconds`); `GET /v1/action-requests/{id}` answers at once. Give it
   `wait_seconds` and `wait_until` with the MCP surface's bounds, satisfaction rule, and
   re-authorization before returning data after a wait. A deadline returns the current receipt.
   When the Action Service moves behind the facade this becomes a server stream of receipts that
   ends at the condition; the `wait` command's contract does not change.
2. **Paging for `GET /v1/action-requests`.** The route returns every matching request, including
   arguments and results, with no limit (`ActionStore.list_requests`). `requests` needs a limit and
   a cursor. `get --key` is unaffected: one key, at most one row.

Not required, but the same change may take it: `GET /v1/action-groups` returns every Action's full
description and input schema inline, so `groups --json` is large. Text mode prints one line per
Action.

## Rollout

The order follows [Settings](../AGENTS.md#settings): code lands before the configuration that uses it.

1. The two Action Service changes above.
2. The CLI, released and carried in the sandbox image (`images/sandbox.nix`), pinned in
   `nix/artifact-pins.json` as the runner wheel is.
3. The environment variables: the URLs and credentials from the deployment, and the runner's
   per-Session `AGENTPLANE_DESTINATION`, which needs the pinned runner to export it. Until then
   `--destination` serves.
4. The platform instructions gain a short paragraph naming `agentplane` and its `--help`, and each
   workflow guide shows the command beside the route it wraps. The service table, URLs and
   placeholders stay, because `curl` remains supported. Sessions started earlier keep their
   original prompt and keep working through `curl`.

The system overview diagrams do not change: the CLI adds no service and no traffic.

## What the implementation must prove

- Every row of the HTTP-to-exit table, against a fake server that sends both refusal headers, and
  every outcome exit of `wait`, `result` and `cancel`.
- A `submit` that loses its response sends one `POST` and at most one lookup, and never a second
  `POST`.
- A `submit` without `--key` writes its generated key to stderr before the `POST`, and `get --key`
  with that key finds the request.
- An Action that `succeeded` exits `0` whatever its result says (a nonzero script exit,
  `isError: true`), and the result is printed untouched.
- Each group works with the other groups' configuration absent.
- A `wait` honors its budget across dropped connections and leaves the Action untouched on
  `SIGTERM`.
- One run through the real proxy on `agentplane-testing`: the environment variables, the CA trust
  and the credential presentation are deployment facts a fake server cannot show.

## Settled decisions

Operator, 2026-10-17:

- `actions submit` and `actions cancel` are part of the CLI, beside the reads, waits and
  subscriptions.
- An Action that did not succeed exits non-zero by default
  ([Errors and exit codes](#errors-and-exit-codes)).
- The idempotency key on `submit` is optional and generated when absent
  ([Submitting](#submitting)).
- The CLI is not an Actions tool: `notifications` and `egress` are groups beside `actions`, under
  the shared rules, and it has no wiring specific to shell-execution Actions yet.
- Build timing is not decided here. When building starts, the CLI is written against the services
  as they are then, and rebased as they change.
- Language is Go or Python in the runner wheel, whichever the implementer picks. Go starts in
  milliseconds where a Python CLI imports Pydantic and httpx on every call and is one static
  binary; Python reuses the wheel's release pin and the existing typer dependency.

Operator, 2026-10-18:

- Exit codes inside a payload's logical content do not reach the CLI's exit code: an MCP tool's
  `isError: true` and a sandbox script's `exit` both complete the Action as `succeeded`
  (`tool_results.py`, `sandbox/executor.py`), so `submit --wait && deploy` proceeds after either.
  This is the conservative start. Propagating some payload verdicts may be added if it proves
  worth doing, as new codes after `17` without renumbering. Until then the guide for a group that
  returns such payloads shows the `jq` check in
  [Errors and exit codes](#errors-and-exit-codes).

## Rejected alternatives

- **MCP as the agent's only route.** One model turn per call and no piping. MCP stays an additional
  surface ([principles](../docs/principles.md#agent-facing-affordances-are-scriptable-apis)).
- **Exit `0` whenever an answer arrives, with an opt-in flag for failure** (as
  `gh run watch --exit-status`). Approval is not execution success, and `execution_unknown` is not
  permission to retry, yet `agentplane actions wait ID && deploy` would proceed on a denied or
  unknown Action. Stdout carries the receipt in every case, for a script that wants the reason.
- **Output mode chosen by TTY detection.** Whether an agent's shell tool allocates a terminal varies
  by harness, so the same command would print different things.
- **Credential discovery from `GET /v1/rules` on each call.** Every command would depend on the
  Egress rules API being up, against [independent failure](../docs/principles.md#components-roll-and-fail-independently).
- **A CLI-owned JSON schema over the service's.** A mirrored type set drifts from the service; the
  service's JSON stays the contract, and a service migration updates the CLI atomically.
- **Polling in the CLI for `wait`, or an `events --follow` over polling.** Waiting is event-driven
  on the server.
