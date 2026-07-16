# airlock

OAuth credential broker for services that need a human to complete an upstream
authorization flow. The Svelte UI shows provider status and starts connect or
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
Airlock FastAPI + Svelte UI             port 8765
  ├── /auth/config                      SPA OIDC configuration
  ├── /api/oauth/providers              provider/token status
  ├── /oauth/authorize/<provider>       upstream authorization redirect
  └── /oauth/callback[/<legacy-name>]   upstream OAuth callback
             │
             ▼
Kubernetes Secrets
  ├── refresh-token Secret              retained only in the airlock namespace
  └── access-token Secret               mirrored to explicit consumers by ESO
```

Browser API requests carry an Authentik JWT. Airlock verifies it against the
issuer's JWKS before returning provider or deployment status.

## Running

```bash
bazel run //airlock:server
```

`CONFIG_PATH` must point to a YAML config file (see below). Provider client IDs
and secrets are supplied as `<PROVIDER_NAME>_CLIENT_ID` and
`<PROVIDER_NAME>_CLIENT_SECRET` environment variables.

## Key modules

| Module                | Purpose                                                         |
| --------------------- | --------------------------------------------------------------- |
| `models.py`           | OAuth provider status and deployment metadata models            |
| `config.py`           | Server and upstream OAuth-provider configuration                |
| `app.py`              | FastAPI app factory, authenticated status API, and uvicorn main |
| `oauth/provider.py`   | Provider configuration plus authorize/token/refresh operations  |
| `oauth/routes.py`     | Browser authorization and callback routes                       |
| `oauth/k8s_client.py` | Kubernetes Secret token storage                                 |
| `oauth/refresh.py`    | Background refresh and orphaned-secret cleanup                  |
| `frontend/`           | Svelte provider-status and connect/reconnect UI                 |

## Configuration

Set `CONFIG_PATH` to a YAML file (default: `/etc/airlock/config.yaml`).

```yaml
public_base_url: https://airlock.example.com
oidc_issuer: https://auth.example.com/application/o/airlock/
oidc_client_id: airlock-operator
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

All other settings live in the YAML config file.
