# Submit an Action and establish its result

Paths on this page are relative to the Actions base URL in your platform instructions. Use its
workload Bearer placeholder through the configured egress proxy.

## Discover and check access

1. Find the operation with `GET /v1/action-groups` (`agentplane actions list`). Fetch its current
   argument schema with `GET /v1/action-groups/{group}/actions/{action}`
   (`agentplane actions show GROUP/ACTION`); do not guess arguments.
2. Inspect `GET /v1/action-policy` (`agentplane actions policy`): if `synced` is true, match the exact Action group/name and
   intended arguments against `auto_approve_if`. Check argument-schema constraints,
   owner/repository constraints, and any server-side repository visibility requirement.
3. A confirmed match is expected to auto-approve. No match, or `synced: false`, requires operator
   approval; an unresolved constraint may require it. Bindings can expire, policy can change, and
   server-side checks can fail. The submitted decision is authoritative. Do not submit requests
   just to discover whether they prompt the operator.
4. Prefer a narrow route using your own Kubernetes permissions, allowed egress credentials, or
   confirmed auto-approval before asking for manual approval. If manual approval is necessary,
   explain why existing access cannot perform the operation and keep the request narrowly scoped.

## Submit once

`POST /v1/action-requests` takes a unique caller-chosen `idempotency_key`, an `action` identity
(`group` and `name`), `arguments` matching the discovered schema, and a human-readable `title`.
Use OpenAPI for the complete envelope and field limits. The operator sees the title and optional
`description`; explain the operation and necessary scope without putting credentials in either.
Keep the key and returned request ID with the task state.

`agentplane actions submit GROUP/ACTION --title TEXT --arguments JSON` does this with a generated
`idempotency_key` that it writes to stderr before sending (`--key` supplies your own). `--arguments`
also takes `@FILE` or `-`; the service validates them against the schema. It prints the receipt,
and `--wait` follows with the wait below.

A repeated submission key is refused, rather than replaying the original response. If the response
is lost, use the [recovery procedure](recovery.md); do not resubmit or invent a replacement key.

## Wait and verify

`decision_pending` means waiting for an operator. An allow decision is not execution success.
Read `GET /v1/action-requests/{id}` for current decision, execution, and result. Retained events are
at `GET /v1/action-requests/{id}/events?after_sequence={last_sequence}`; advance that sequence only
after recording the returned events, and page after the last sequence when needed. Your requests,
newest first, are at `GET /v1/action-requests`, optionally with `state`; pass `limit` (at most 128)
and, for the next page, `after_id` set to the last request ID you already have.

Add `wait_seconds` (a number, at most the instance's maximum; over it is refused) and `wait_until`
(`decision` or `terminal`, the default) to the receipt read to hold it until that condition. A
deadline returns the current receipt with status 200, which is not a failure and does not cancel
anything. `agentplane actions wait {ID | --key KEY} [--until decision|terminal]` does this within
`--timeout` (30 seconds by default) and `agentplane actions result` waits for `terminal` and prints
the stored result; `agentplane actions requests` and `agentplane actions events ID --after SEQUENCE`
page the two lists above.

Exit status is the platform's answer, never the Action's own output: `0` means the awaited condition
holds and the Action succeeded; `10` the deadline passed first (the receipt is on stdout); `11`
denied; `12` failed without a result; `13` cancelled; `14` `execution_unknown`. A script's own exit
code is in `execution.result` of a `succeeded` Action, so read it there. `agentplane actions --help`
lists the rest. A short synchronous wait is reasonable for an immediately finishing operation. For approval waits,
longer execution, or parallel work, use the Notifications service's `/llms.txt` and subscription
workflow. You may subscribe immediately. An already-finished Action needs a result read, not a new
subscription. Notifications replay Action history, so completion between submit and subscribe is
recoverable. A healthy subscription replaces repeated polling; always inspect the Action's own
API before proceeding with dependent work.

Report denial or failure and its observed reason. Treat `execution_unknown` as uncertain execution,
not as permission to retry. If changing plans, read the cancellation procedure first. A wait timeout,
disconnect, or ended agent turn does not withdraw a request. Do not decide an Action yourself or
call operator decision routes.
