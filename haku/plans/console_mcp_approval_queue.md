# Plan: operator-approved MCP tool calls through haku-console

Status: implementation plan for the v1 approval-gated tool-call path.

## Goal

Give Haku and haku-ui a shared escape hatch for exact MCP tool calls that Haku should
not run autonomously. Haku may propose any tool on any MCP server connected to
haku-console. In v1, every proposed call requires trusted operator approval in
haku-console before execution.

haku-console owns the connected-server registry, approval ledger, execution, audit log,
idempotency, and result store. haku-state stores authored requests and UI affordances
only; it does not mirror `tool_results/`. Haku can query or sweep haku-console's audit
API during its normal pass and reduce executed calls into ordinary state files.

## Console Contract

Connected MCP servers are configured in ducktape as data mounted with
`configMapGenerator`:

```yaml
servers:
  - id: grocy-sf
    server_url: https://grocy-mcp-sf.allegedly.works/mcp
    bearer_token_secret: haku-console-grocy-sf-token
```

This is not an operation allowlist. haku-console reflects each server's live MCP
`tools/list` response and exposes those tools to callers.

Core endpoints:

- `GET /api/capabilities/mcp-servers` returns connected server IDs and reflected tool
  metadata, including MCP-published input schemas when available.
- `POST /api/tool-calls` accepts `server_id`, `tool_name`, `arguments`,
  `rationale`, optional `title`, optional `state_request_id`, optional
  caller-scoped `client_request_id`, and optional `wait_for_ms`.
- `GET /api/approvals/pending`, `GET /api/approvals/events?since=...`, and
  `WebSocket /api/approvals/ws` provide frontend catch-up and wakeups.
- `POST /api/tool-calls/{tool_call_id}/decision` is CSRF-gated trusted-console approval
  or denial.
- `GET /api/tool-calls`, `GET /api/tool-calls/{tool_call_id}`, and
  `GET /api/tool-calls/by-client-request/{client_request_id}` expose the console-owned
  audit/result log.

haku-console mints the canonical `tool_call_id`. A caller may send a scoped
`client_request_id` only to make retries idempotent. Replaying the same caller/client ID
with the same payload returns the original record; replaying it with a different payload
rejects.

Result statuses are shared across haku-ui, Haku, and future console-MCP clients:
`pending_approval`, `running`, `ok`, `error`, and `denied`.

## haku-state Contract

Haku authors request files under:

```text
tool_requests/<state_request_id>.yaml
```

Request shape:

```yaml
state_request_id: 2026-07-thrive-box-grocy-stock-add
server_id: grocy-sf
tool_name: stock_add
title: Add arrived Thrive box items to Grocy
rationale: The box is physically present; these entries are still in kitchen.board incoming.
arguments:
  items:
    - product_id: 123
      amount: 1
```

haku-ui renders reusable affordances in authored Markdown/MDX:

```html
<tool-call request="2026-07-thrive-box-grocy-stock-add" label="Add arrived Thrive box to Grocy"></tool-call>
```

Click flow:

1. haku-ui frontend reads `tool_requests/<state_request_id>.yaml` and posts that exact
   request body to haku-ui backend.
2. haku-ui backend derives
   `client_request_id = haku-state:tool-call:<state_request_id>`, and submits to
   haku-console.
3. haku-console records the pending call, emits an approval event, and returns the
   console record.
4. haku-console frontend renders the trusted approval prompt and posts approve/deny.
5. haku-console executes or denies the MCP call and stores the terminal result.
6. haku-ui displays the returned record; later Haku runs can query console audit state
   and reduce terminal outcomes into haku-state.

## Deployment Stages

1. haku-console: server registry reflection, approval endpoints, trusted approval UI,
   durable Postgres console ledger, API token, and MCP credentials.
2. haku-state-template: generic request schema, haku-ui backend proxy endpoint,
   `<tool-call>` affordance, validation, and documentation.
3. live haku-state: same haku-ui wiring, generated frontend API types, deployment env
   pointing at haku-console, and optional authored requests once exact MCP arguments
   are known.
4. deploy: Flux applies console, haku-state Terraform provides the shared API token,
   haku-ui rolls out, and a harmless mock/smoke call proves approval, execution, and
   audit reads before a Grocy write.

## Acceptance Checks

- Haku can list connected MCP servers and reflected tool schemas through haku-console.
- haku-ui and Haku submit the same request shape.
- Every v1 call becomes `pending_approval` until the trusted console frontend approves
  or denies it.
- The console frontend can recover missed approval events via REST catch-up.
- The same `client_request_id` cannot execute a non-idempotent call twice.
- Terminal results are visible in haku-console without a git mirror.
- haku-state validation rejects malformed `tool_requests/*.yaml`.
- A later Haku pass can sweep console audit records and update real state files.
- Future Grocy-specific improvement: investigate whether haku-console can execute Grocy
  calls under the operator's Authentik identity instead of only the console-held service
  token.

## Current Grocy Principal

The Grocy SF registry entry uses a dedicated `haku-console` machine token, not Haku's
read-only Grocy token. The matching Grocy user is policy-managed as ADMIN, and the token
is mirrored only into the trusted `haku-console` namespace. Haku's autonomous Grocy user
remains read-only.

## Later

Expose the same contract as a haku-console MCP proxy. Calls that pass future auto-allow
logic can execute immediately; all other calls remain approval-gated or can be punted to
the asynchronous haku-ui affordance path.
