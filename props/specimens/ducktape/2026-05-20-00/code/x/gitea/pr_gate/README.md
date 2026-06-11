# Gitea PR Gate

Blocks PR creation while allowing normal dev flows (push, branch, comment, review, merge, close/reopen).

## Components

- **`nginx/gitea_pr_gate.conf`**: Reverse proxy allowlist. Blocks PR creation endpoints, allows everything else. Uses `auth_request` to call the quota policy server.
- **`hooks/pre-receive-deny-refs-for`**: Pre-receive hook blocking AGit `refs/for/*` PR creation (covers SSH pushes that bypass the HTTP proxy).
- **`policy_server_fastapi.py`**: Per-user PR quota enforcement via FastAPI.

## Deploy

1. Place nginx config, adjust `server_name` and upstream
2. Install pre-receive hook globally (`/var/lib/gitea/custom/hooks/pre-receive.d/deny-refs-for`) or per-repo
3. Start policy server: `uvicorn gitea_pr_gate.policy_server_fastapi:app --host 127.0.0.1 --port 9099`

## Policy Server Config

| Env Var                | Purpose                                           | Default                  |
| ---------------------- | ------------------------------------------------- | ------------------------ |
| `GITEA_BASE_URL`       | Gitea URL                                         | `http://127.0.0.1:3000/` |
| `GITEA_ADMIN_TOKEN`    | Token for counting PRs in private repos           |                          |
| `PRQ_DEFAULT_MAX`      | Max open PRs per user                             | `3`                      |
| `PRQ_PER_REPO`         | JSON map `{"owner/repo": N}` for per-repo limits  |                          |
| `PRQ_EXEMPT_USERS`     | Comma-separated exempt users                      |                          |
| `PRQ_TRUST_PROXY_USER` | Trust `X-Original-User` header from reverse proxy | `false`                  |

User identification: policy server calls `GET /api/v1/user` with forwarded `Cookie`/`Authorization`. Prometheus metrics at `/metrics`.
