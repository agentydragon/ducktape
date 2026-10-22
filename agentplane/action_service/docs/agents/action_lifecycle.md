# Submit an Action and establish its result

The commands are `agentplane actions …`; each command's `--help` lists its flags and exit statuses.
Without the command, use the generated [OpenAPI reference](/openapi.json) for HTTP routes and schemas.

## Discover and check access

1. Find the operation with `agentplane actions list`. Fetch its current argument schema with
   `agentplane actions show GROUP/ACTION`; do not guess arguments.
2. Inspect `agentplane actions policy`: if `synced` is true, match the exact Action group/name and
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

`agentplane actions submit GROUP/ACTION --title TEXT --arguments JSON` takes `arguments` matching
the discovered schema (`@FILE` or `-` read them from a file or stdin; the service validates them). The
operator sees the title and optional `--description`; explain the operation and necessary scope
without putting credentials in either. It generates an idempotency key and writes it to stderr before
sending (`--key` supplies your own), then prints the receipt; `--wait` follows with the wait below.
Keep the key and the returned request ID with the task state.

A repeated submission key is refused, rather than replaying the original response. If the response
is lost, use the [recovery procedure](recovery.md); do not resubmit or invent a replacement key.

## Wait and verify

`decision_pending` means waiting for an operator. An allow decision is not execution success.
`agentplane actions get ID` reads the current decision, execution, and result. Advance
`agentplane actions events ID --after SEQUENCE` only after recording the returned events.
`agentplane actions requests` lists yours, newest first, optionally by `--state`.

`agentplane actions wait {ID | --key KEY} [--until decision|terminal]` holds until that condition
within `--timeout` (30 seconds by default), and `agentplane actions result` waits for `terminal` and
prints the stored result. A deadline prints the current receipt and exits `10`; it is not a failure
and does not cancel anything.

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
