# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

## Agent records

- **Fuller Agent detail** — `AgentView` carries name, status, credential kind/status, and the
  creation/activation/last-seen times. Client software, granted scopes, and reconnect history are
  in the durable graph and are not yet surfaced.
- **Agent lifecycle controls** — expose revoke/disable, rename/history, and tombstone/reconnect
  operations as vertical API + UI + audit-event slices.

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

## Retained `vector` extension

Nothing at head uses pgvector, but the 0135 baseline still creates a `halfvec` column on a fresh
database, so CNPG (`cluster/cdk8s/haku/database.py`) and the test fixtures keep provisioning it.
Drop it, with the `agentplane/indexing:vector_type` import, when a new baseline replaces 0135.
