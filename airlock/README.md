# airlock

OAuth credential broker for services that need a human to complete an upstream
authorization flow. The React and Mantine UI shows provider status and starts connect or
reconnect flows; the server stores refresh and access tokens in Kubernetes
Secrets and refreshes access tokens in the background.

Airlock has no MCP endpoint, tool proxy, action queue, or operator tool-approval
API. Haku Console owns the live risky-MCP-tool policy, approval, audit, and result
flow; see <../haku/console/README.md>.

## Architecture

```text
Operator browser
  │  Authentik login (Authorization Code + PKCE)
  ▼
Airlock FastAPI + React/Mantine UI       port 8765
  ├── /auth/login and /auth/callback    backend OIDC flow and signed session cookie
  ├── /api/oauth/providers              provider/token status
  ├── POST /oauth/authorize/<provider>  authenticated upstream authorization redirect
  └── /oauth/callback[/<legacy-name>]   upstream OAuth callback
             │
             ▼
Kubernetes Secrets
  ├── refresh-token Secret              retained only in the airlock namespace
  └── access-token Secret               mirrored to explicit consumers by ESO
```

The backend exchanges the OIDC authorization code and validates the issuer,
audience, subject, username, nonce, and expiry. After login, browser API requests
use a same-origin `HttpOnly`, `SameSite=Lax` session cookie containing only the
operator identity and expiry (`Secure` on HTTPS deployments). The Authentik
access token stays on the server and is not needed after login. OAuth connection
starts require an exact same-origin POST and callbacks must match the authenticated
operator session.

## Running

```bash
bazel run //airlock:server_bin
```

Config is YAML at `CONFIG_PATH` (default `/etc/airlock/config.yaml`; `session_seconds` caps the
browser session, default 8 h):

```yaml
public_base_url: https://airlock.example.com
oidc_issuer: https://auth.example.com/application/o/airlock-server/
port: 8765
oauth:
  target_namespace: airlock
  managed_by: airlock
  providers:
    - name: example
      provider_type: oauth2
      display_name: Example
      authorize_url: https://provider.example.com/oauth/authorize
      token_url: https://provider.example.com/oauth/token
      scopes: [read]
      refresh_secret:
        name: example-refresh-token
      access_secret:
        name: example-access-token
```

Required env: `AIRLOCK_OIDC_{CLIENT_ID,CLIENT_SECRET,SESSION_SECRET}` (from the
`airlock-oidc-config` Secret provisioned by `tf/gitops/sso-providers`; the last signs the session
cookie), and per provider `<NAME>_CLIENT_ID` / `<NAME>_CLIENT_SECRET`, where `<NAME>` is the
provider `name` uppercased with `-` → `_` (`google-write` → `GOOGLE_WRITE_CLIENT_ID`).

Code: `auth.py` (Authentik code flow, sessions), `oauth/` (`provider.py` authorize/token/refresh,
`routes.py`, `k8s_client.py` Secret storage, `refresh.py` background refresh and orphaned-Secret
cleanup), `frontend/`.
