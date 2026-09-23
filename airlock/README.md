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
bazel run //airlock:server
```

`CONFIG_PATH` must point to a YAML config file (see below). Authentik client
credentials and the session signing key are read from the `airlock-oidc-config`
Kubernetes Secret provisioned by `tf/gitops/sso-providers`. Upstream OAuth
provider client IDs and secrets are supplied as `<PROVIDER_NAME>_CLIENT_ID`
and `<PROVIDER_NAME>_CLIENT_SECRET` environment variables.

## Key modules

| Module                | Purpose                                                          |
| --------------------- | ---------------------------------------------------------------- |
| `models.py`           | OAuth provider status and deployment metadata models             |
| `config.py`           | Server and upstream OAuth-provider configuration                 |
| `app.py`              | FastAPI app factory, session middleware, and uvicorn main        |
| `auth.py`             | Authentik code flow, session creation, and browser authorization |
| `oauth/provider.py`   | Provider configuration plus authorize/token/refresh operations   |
| `oauth/routes.py`     | Browser authorization and callback routes                        |
| `oauth/k8s_client.py` | Kubernetes Secret token storage                                  |
| `oauth/refresh.py`    | Background refresh and orphaned-secret cleanup                   |
| `frontend/`           | React and Mantine provider-status and connect/reconnect UI       |

## Configuration

Set `CONFIG_PATH` to a YAML file (default: `/etc/airlock/config.yaml`).

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

### Environment variables

| Variable                        | Required | Description                                                   |
| ------------------------------- | -------- | ------------------------------------------------------------- |
| `CONFIG_PATH`                   | no       | Path to YAML config file (default `/etc/airlock/config.yaml`) |
| `<PROVIDER_NAME>_CLIENT_ID`     | yes      | Client ID for each configured provider                        |
| `<PROVIDER_NAME>_CLIENT_SECRET` | yes      | Client secret for each configured provider                    |
| `AIRLOCK_OIDC_CLIENT_ID`        | yes      | Authentik client ID from `airlock-oidc-config`                |
| `AIRLOCK_OIDC_CLIENT_SECRET`    | yes      | Authentik confidential client secret                          |
| `AIRLOCK_OIDC_SESSION_SECRET`   | yes      | Signs the Airlock browser session cookie                      |
| `session_seconds` in YAML       | no       | Maximum browser session lifetime (default 8 hours)            |

All other settings live in the YAML config file.
