# Operator browser auth audit — why old console tabs "complain about auth"

Reported symptom: returning to a haku-console tab that has been open for a while shows an auth
error, or otherwise behaves oddly.

Scope: the operator browser login flow (`operator_auth.py`, `frontend/client.ts`,
`frontend/operator_login.ts`, `frontend/console_events.ts`, `console_events.py`). Agent `/mcp`
admission and the account-link OAuth flows were reviewed only where they touch the browser session.

Two of the findings below were reproduced against the real app (`create_app` + the hermetic mock
IdP from `util.testing.mock_oidc`, served over real sockets); the probe output is quoted inline.

## The chain that produces the symptom

1. The operator session carries an absolute, non-sliding deadline of one hour
   (`OPERATOR_SESSION_MAX_AGE_SECONDS`). This is deliberate and covered by
   `test_continuous_use_cannot_slide_operator_session_past_absolute_deadline`.
2. Every open tab therefore loses its session at roughly the same moment, an hour after login.
3. Each tab notices on its own within ≤30 s — `useConsoleEvents` syncs on a 30 s interval and the
   server closes its `/api/events/ws` on its own 30 s revalidation tick — and each tab
   independently calls `redirectToOperatorLogin()`, i.e. `location.replace("/auth/login")`.
4. Those concurrent `/auth/login` requests **overwrite each other's OAuth state** (F1). All but one
   tab lands on the backend-rendered dead end: _"Operator login failed — This login attempt expired
   or was superseded by a newer attempt."_
5. The tab that does re-authenticate comes back at `/`, having lost whichever view it was on (F2),
   and the top-level navigation destroyed the framed haku-ui's state along the way (F3).

## F1 — Concurrent login attempts clobber each other's OAuth state (confirmed)

`build_oauth` uses authlib's Starlette integration, which keeps pending-login state in the signed
session cookie as `_state_authentik_<state>`. Starlette's `SessionMiddleware` serializes the whole
session **snapshot it read at request start** into a `Set-Cookie` on every modifying response. Two
`/auth/login` requests in flight at once both read the same snapshot, so the second `Set-Cookie` to
land silently drops the first request's state.

Reproduced with one cookie jar and two overlapping `/auth/login` calls (two tabs of one browser):

```text
PROBE start_a 302 start_b 302
PROBE jar after both logins: {'session': '...eyJfc3RhdGVfYXV0aGVudGlrX2ZpM2V6aWxqYVhkS...'}   # ONE state, not two
PROBE tab B final 404 http://127.0.0.1:46727/          # completed, logged in, redirected to /
PROBE tab A final 401 .../auth/callback?code=...&state=EgOzWbAbosaYwbelTZ1ot1v04nm9mC
PROBE tab A body: <title>Operator login failed</title>
PROBE /auth/me after both: 200 {"username":"agentydragon"}
```

Exactly one of two simultaneous attempts survives; the loser gets the 401 page. With N stale tabs
bouncing together, N−1 lose. The failure page is a genuine dead end: `script-src 'none'`, so it
cannot retry itself, and its `Retry login` link starts a fresh flow that returns to `/`, not to
where the tab was.

Every other OAuth flow in the console already avoids this by keeping flow state in Postgres keyed by
`state` — `mcp_operator_oauth_flows` and `provider_connection_flows` in `database_schema.py`. The
operator login flow is the only one that still keeps it in the cookie.

### F1b — A login attempt left open for over an hour fails the same way

`set_state_data` stamps each state entry with `exp = now + 3600`, and the session cookie's own
`Max-Age` is 3600. So a tab that was bounced to Authentik and left sitting there (screen locked,
laptop closed, tab forgotten) produces the identical _"expired or was superseded"_ page when the
login is finally completed. This one needs no race at all, and matches "I come to old tabs and they
complain" just as well as F1 does.

## F2 — Re-login always lands at `/`, losing the tab's view

`redirectToOperatorLogin()` sends the browser to a bare `/auth/login` with no continuation, and
`_validated_enrollment_return_to` would reject anything that is not
`/auth/agent-enrollment/<uuid>` with a 400 anyway. The callback's fallback is `/`.

Consequences: a tab on `/_console/tool-calls` or `/_console/settings` comes back on the embedded
Haku UI, and a tab deep in a haku-ui route comes back at the haku-ui root.
`rememberedEmbedPath()` does not rescue it — `useConsoleView` only consults it when the pathname is
exactly `/_console`, and `routeFromLocation("/")` returns `/`.

The strict validation itself is right (it is an open-redirect guard). What is missing is a second
accepted shape: any same-origin console path.

## F3 — The hourly expiry yanks the tab away with no warning

The absolute deadline is a deliberate security property and should stay. The client-side handling
of it is what makes it feel broken:

- The redirect is a **top-level `location.replace`**, so the cross-origin haku-ui iframe is torn
  down along with anything unsaved in it.
- It fires from a **background poll**, not from an operator action — a tab nobody is touching
  navigates itself to Authentik, and if the Authentik SSO session has also lapsed it parks on a
  login form.
- Nothing warns beforehand. `/auth/me` returns only `username`, so the frontend cannot know the
  deadline even if it wanted to.

Related doc drift: the comment at `operator_auth.py:217-219` says `SessionMiddleware` refreshes the
cookie timestamp whenever it serializes the session. Under the pinned `starlette==1.3.1` that is no
longer true — the middleware only writes a cookie when `session.modified` is set, so plain reads no
longer re-sign it and the cookie itself now expires an hour after login. The independently signed
`expires_at` is still correct and still worth keeping (it is what makes the deadline explicit rather
than a middleware implementation detail), but the stated reason for it is stale.

## F4 — The event socket reports an expired session as "operator is disabled or missing"

`console_events_ws` closes with `1008 "operator is disabled or missing"` whenever
`operator_session(...)` returns `None`, which includes ordinary expiry. Two effects:

- The reason is misleading in logs and in anything that surfaces it.
- The reconnect handshake is answered with a WebSocket denial response (confirmed: probe raised
  `WebSocketDenialResponse <Response [401 Unauthorized]>`), which browsers do not expose to JS. So
  the shell only shows the crossed-wifi _offline_ indicator; recovery depends entirely on the REST
  `sync()` that `onclose` happens to fire. Until that lands, the tab looks like it has a network
  problem rather than an auth problem.

`CloseEvent.code`/`reason` _are_ available to the client for a server-initiated close, so this is
directly fixable: close expired sessions with their own reason and let `console_events.ts` route
that to the login flow instead of the offline indicator.

Minor: an abandoned tab keeps reconnecting on a 30 s-capped backoff forever, and each attempt costs
a `resolve_active_session` query.

## F5 — Minor: abandoned login states accumulate in the cookie

authlib only sweeps expired `_state_authentik_*` entries as a side effect of a _successful_
`clear_state_data`. Each abandoned attempt leaves a ~300-byte entry behind for an hour. Bounded by
the 1 h cookie lifetime, so this is not the reported symptom, but it is one more argument for moving
login flow state out of the cookie.

## F6 — Minor: `/auth/logout` has no caller and no IdP logout

The endpoint exists and is correctly exact-Origin gated, but nothing in the SPA calls it — there is
no sign-out affordance. It also clears only the console session, not the Authentik SSO session, so a
manual logout would silently re-login on the next 401.

## What is solid

Worth recording so a future change does not undo it:

- Every browser mutation and the event socket require the console's exact `Origin`.
- The callback pins the verified issuer back to configured trust, so discovery cannot substitute an
  issuer, and it requires a non-empty `sub` and a resolvable authorized identity.
- Every request revalidates the session against the DB (`resolve_active_session`), so disabling an
  Operator takes effect immediately rather than at cookie expiry.
- `return_to` is validated as a local enrollment path only — no open redirect.
- An explicit `Authorization` header always selects Agent admission on `/mcp`; an invalid bearer
  never falls back to the ambient browser cookie.
- Account-link OAuth flows already keep their flow state server-side, which is exactly the pattern
  F1 needs.

## Status

F1/F1b, F2, F3, F4 and F5 were fixed in the same change that added this note; the recommendations
below are what was implemented. F6 (no sign-out affordance) is untouched.

While implementing, F1 turned out to be **worse than the reproduction above shows**: authlib's
Starlette integration clears every prior `_state_<name>_*` entry each time it stores a new one, so a
second tab strands the first one _without_ any race at all. The Postgres-backed flow table removes
the whole class.

## Recommended fixes, in order

1. **Move login flow state to Postgres** (fixes F1 and F1b, and F5). Subclass authlib's
   `FrameworkIntegration` and back `get_state_data` / `set_state_data` / `clear_state_data` with a
   short-lived table keyed by `state`, mirroring `provider_connection_flows`. Concurrent logins then
   have independent rows and cannot evict one another, and the entry's lifetime stops being tied to
   the session cookie.
   _Cheap interim mitigation if the migration is not wanted yet:_ on `mismatching_state`, restart the
   login once automatically (redirect to `/auth/login` with a one-shot marker) instead of rendering
   the dead end. With a live Authentik SSO session the recovery is invisible.
2. **Carry the operator back to where they were** (fixes F2). Accept a second `return_to` shape —
   same-origin path, no scheme/netloc, not under `/auth/`, existing length and control-character
   checks — and have `redirectToOperatorLogin()` pass `location.pathname + location.search`.
3. **Make expiry visible instead of abrupt** (fixes F3). Expose the deadline on `/auth/me` and let
   the shell warn shortly before it, so re-authentication happens on an operator gesture rather than
   from a background poll that discards the iframe.
4. **Distinguish expiry from disablement on the socket** (fixes F4), and let the frontend act on the
   close code rather than waiting for the next REST sync.
5. Optional: add a sign-out affordance, with RP-initiated logout at Authentik (F6).
