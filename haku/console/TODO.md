# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

## Notification text per tool kind

A push notification is titled with the tool's shared action description
(`frontend/tool_rendering/<server>/actions.ts`) — the same one-line summary the approvals card's
identity line shows. That is the right default, but a notification is a different surface: no
arguments visible, no expand affordance, read on a lock screen, and it is the one place a call
can be approved without seeing its arguments at all. Some tools would be better served by
notification-specific wording — naming the actual target ("Delete Pod haku-console-7f9 in
haku-console") where the card can rely on the widget below it to show that.

Add an optional per-tool notification override alongside the action description, falling back to
it when absent. Deliberately not done in the change that introduced push: the shared description
is the honest starting point, and which tools actually warrant divergence is worth learning from
real notifications rather than guessing up front.

## MCP server (`/mcp`) — deferred follow-ups

The `/mcp` server (`mcp/server.py`) now resolves canonical Operators, Agents, grants, and
credential bindings through one authority, and derives each request's tool surface from that
Agent's Operator connections. Settings lists the Operator's Agents and lets an OAuth Agent's
auto-approval policy be reassigned among the roots `config.yaml` defines. The architecture is
specified in <../../plans/oauth_architecture.md>. The next product slices are:

- **Fuller Agent detail** — `AgentView` carries name, status, credential kind/status, and the
  creation/activation/last-seen times. Client software, granted scopes, and reconnect history are
  in the durable graph and are not yet surfaced.
- **Agent-filtered history** — filter past tool calls by Agent only after applying the
  authenticated Operator predicate. Resolve display names through canonical joins; never copy
  them into tool-call rows or use them as authority.
- **Agent lifecycle controls** — expose revoke/disable, rename/history, and tombstone/reconnect
  operations as vertical API + UI + audit-event slices.
- **Author a policy in the UI** — reassignment picks among deploy-defined roots; composing a typed
  structured policy in the console is what remains.
- **Per-Agent tool surface** — derive request-time `list_tools` from the verified binding and
  policy, with `tools/list_changed` on policy edits. Do not key authorization directly on an
  unverified DCR `client_id`.

## Operator browser auth — parked remainders

The browser login flow is fixed in #3516/#3519 except for:

- **A background 401 still navigates the tab** (audit F3). Expiry is now announced beforehand and
  re-authentication returns to the same page, but the redirect itself is still fired by whichever
  poll happens to fail first, and the top-level navigation discards whatever is unsaved in the
  framed haku-ui. The alternative is an explicit "session expired — sign in" state the operator
  clicks, so the frame survives until they choose. Superseded entirely if session renewal lands
  (<plans/operator_session_renewal.md>).
- **No sign-out affordance** (audit F6). `/auth/logout` exists and is exact-Origin gated, but
  nothing in the SPA calls it, and it clears only the console session — not Authentik's — so a
  manual logout silently re-logs-in on the next 401. Needs RP-initiated logout to be meaningful.

## Recall indexing is disabled

The deployed console intentionally leaves Recall index configuration, the `haku_index` MCP server,
and index-maintenance workers unwired. The `recall_index` database schema and data remain for a
future re-enable; restore source/embedding workers and review catalog/access-profile exposure
together when that work resumes.

## Small cleanups

- `approval_mode` (`ApprovalMode` in `haku/shared/haku/console/tool_calls.py`, mirrored on
  `mcp_approval.ToolMetadata`) conflates "which input-schema shape does the proxy tool advertise"
  (enveloped vs raw) with "does a call auto-approve". They happen to map roughly 1-1 today, but
  the interface should not encode that coupling — split the schema-shape signal from the
  approval-policy signal.
