# Auth security review — app-owned operator OIDC + agent-facing MCP server

Security review of the haku-console change that gives the console a native `/mcp` server and
moves operator authentication into the app (Authentik OIDC session), retiring the Authentik
forward-auth proxy outpost. Scope: the auth mechanisms between the operator, the AI-agent
clients (claude.ai / `claude` CLI), haku-console, the backend MCP servers, and Authentik.

One HIGH-severity issue was found and confirmed by adversarial verification. The rest of the
new auth surface was examined and cleared (summary at the end).

> **Status: RESOLVED.** Finding 1 was fixed in #3088 and landed on `devel` as part of #3054: the
> `x-authentik-*` fallback is removed and operator identity is session-only. The code excerpts and
> `operator_auth.py:88-110` line references below describe the state **at review time** (pre-fix).
> Remaining optional hardening (not required now that the fallback is gone) is listed under
> [Fix](#fix).

## Finding 1 (HIGH): authentication bypass via spoofed `x-authentik-*` request headers

`haku/console/operator_auth.py:88-110` (at review time — the fallback shown below is now removed)

### What

The change retires the Authentik forward-auth outpost that used to gate
`haku.allegedly.works` and points the `HTTPRoute` (`cluster/k8s/haku/console/httproute.yaml`)
straight at the console Service, so unauthenticated internet traffic reaches the app directly.
The app is meant to authenticate the operator via an Authentik OIDC signed-session cookie,
enforced by the router guards `require_operator` and `require_operator_or_static_agent`.

Both guards resolve identity through `operator_subject(conn)`, which falls back to trusting a
**client-supplied header** whenever the session has no authenticated user:

```python
# haku/console/operator_auth.py:88-102
def operator_subject(conn: HTTPConnection) -> str | None:
    if "session" in conn.scope:
        user = conn.session.get(SESSION_USER_KEY)
        if isinstance(user, dict) and isinstance(subject := user.get("subject"), str):
            return subject
    return conn.headers.get("x-authentik-uid")   # <-- always active
```

In production `operator_oidc` is set, so `SessionMiddleware` is installed and `"session"` is
always in scope — but an unauthenticated request has an empty session dict, so `user` is
`None` and control falls through to the header read. The guard's gate `_app_owned_auth(conn)`
only decides _whether the guard runs_, not _whether the header fallback is used_:

```python
# haku/console/operator_auth.py:170-176
def require_operator(conn: HTTPConnection) -> None:
    if _app_owned_auth(conn) and operator_subject(conn) is None:
        raise HTTPException(status_code=401, detail="operator authentication required")
```

Any non-`None` header value satisfies the guard, and nothing strips the header inbound:

- The `HTTPRoute` has **no** `RequestHeaderModifier` filter (unlike the Authentik
  proxy-routes).
- nginx (`haku/console/default.conf.template`) proxies `/api/` and `/auth/` setting only
  `Host`/`X-Real-IP`/`X-Forwarded-*`; it never clears inbound `x-authentik-uid` /
  `x-authentik-username` (hyphenated names, so nginx's `underscores_in_headers`-off default
  does not drop them).
- No `NetworkPolicy` exists in `cluster/k8s/haku/console/` — the console kustomization lists
  only `csrf-secret`, `deployment`, `httproute`, `routine-launch-token`, `service`.
- No global gateway-level strip of `x-authentik-*` was found.

`operator_username` has the same fallback at `operator_auth.py:110`.

This violates the repository's own documented invariant for the sibling services that trust
these headers: `x-authentik-*` identity is trustworthy **only** when the outpost sets it **and**
a `NetworkPolicy` restricts ingress to the outpost — see <../docs/containment.md> (Operator
identity — forward-auth headers, made trustworthy), <../../../cluster/docs/sso.md>
(Proxy-mode NetworkPolicy), and the enforced `cluster/k8s/haku/namespace/networkpolicy.yaml`
for haku-ui. haku-console removes the outpost, keeps a header-trusting fallback, and adds no
`NetworkPolicy` — while being exposed to the public internet.

### Impact

An unauthenticated internet attacker sends any request to `https://haku.allegedly.works` with a
forged `X-Authentik-Uid` header and passes `require_operator`, reaching the entire
operator-only surface (pending approvals, tool-call submission, MCP-server reflection,
operator-OAuth connect/disconnect). Amplified to full execution:

1. `GET /api/capabilities/csrf` (behind `require_operator`, now passed) both **sets** the
   signed double-submit CSRF cookie and **returns** the matching token in one response
   (`capabilities.py:55-61`). A direct (non-browser) attacker who controls both halves defeats
   the double-submit CSRF check entirely.
2. `POST /api/tool-calls` — submit an arbitrary tool call.
3. `POST /api/tool-calls/{id}/decision` — self-approve it. For any server **not** using
   `operator_oauth`, `_execution_auth` returns the console's **own** stored credential
   (`mcp_approval.py:492-503`). The in-process `gmail` / `google_calendar` / `haku_routine`
   servers execute against the console's Airlock-issued `haku_console_google` token
   (`gmail.modify` + `gmail.compose` + `calendar.events`) and the `haku-routine-launch-token`.

So — without ever knowing the operator's real Authentik subject — the attacker can read the
operator's Gmail, create drafts, relabel mail, create calendar events, and fire the Haku
claude-code-web routine. For `operator_oauth`-backed servers (e.g. a kubectl-passthrough
running as the operator's cluster-admin identity), the attacker additionally needs to set
`X-Authentik-Uid` to the operator's real opaque `sub` to hit an existing token association
(`access_token_for`, keyed on `operator_subject`) — a higher bar, but the static-credential
tier above needs no such knowledge and is already critical.

### Newly introduced by this change

`httproute.yaml` and `operator_auth.py` are new. Pre-change the only public path was
gateway → outpost, and the header-trusting fallback did not exist in a deployed form. Net
effect: outpost removed, header-trusting fallback added, no `NetworkPolicy` added.

### Fix

Drop the `x-authentik-*` fallback completely — it is a proxy-auth holdover. Operator identity
comes only from the app-owned OIDC session; `operator_subject` / `operator_username` return the
session value or `None`.

**Applied in #3088** (on `devel` via #3054): both fallbacks removed, tests switched from the
`x-authentik-*` header stand-in to a real injected operator session. The bypass is closed — a
forged `x-authentik-*` header is now ignored.

**Remaining optional hardening** (defense-in-depth; not required now that the app ignores the
header, but would bring haku-console in line with the sibling services' documented pattern):

- an edge `RequestHeaderModifier` on `cluster/k8s/haku/console/httproute.yaml` that strips inbound
  `x-authentik-*`, and/or clearing them in nginx, so the header can never reach the app;
- a `NetworkPolicy` on the `haku-console` namespace restricting Service ingress to the gateway,
  matching `cluster/k8s/haku/namespace/networkpolicy.yaml` (haku-ui) and the grocy/scanner
  policies.

## Cleared during review

The following new auth surfaces were examined and found not to be exploitable:

- `operator_subject_from_idp_tokens` decoding the `id_token` with `verify_signature=False`
  (`mcp_operator_oauth.py:84`) — the token is server-to-server from Authentik during the code
  exchange, not client-supplied; a missing `sub` fails the exchange closed.
- `_public_base_url()` deriving `redirect_uri` from `Host` / `X-Forwarded-Host`
  (`mcp_operator_oauth.py:386`) — production sets `HAKU_CONSOLE_PUBLIC_BASE_URL`, and
  authorization servers strict-validate `redirect_uri` with PKCE binding the code.
- OAuth connect/disconnect CSRF, the unguessable server-stored `state`, and PKCE.
- The callback HTML template — `html.escape` on `$title` / `$message`, fixed-literal
  `$payload`.
- The `haku/`-prefix Gmail auto-approval boundary (`auto_approval.py`) — label id → name
  resolution, schema-validated, fail-closed, `kubectl-passthrough-mcp` excluded from
  unconditional approval.
- Operator/agent resolution in `mcp_approval.py` — fail-closed 409 when unlinked,
  composite-key token lookup, no `/mcp` self-approval tool.
- The static-bearer → `client_id` mapping and the new `on_client_authorized` hook (propagates
  exceptions, fail-closed).
- `mcp_infra/persistence.py`, `tf/gitops/agent-machine-access/main.tf` (confidential clients,
  strict redirect URIs, single-user group binding), and the test-only
  `util/testing/mock_oidc.py` / `asgi.py` (not reachable from production).
