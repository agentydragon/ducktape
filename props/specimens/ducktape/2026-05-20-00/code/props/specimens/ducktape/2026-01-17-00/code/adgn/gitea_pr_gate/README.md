Gitea PR Gate (Proxy Allowlist + Pre-Receive Hook)

Purpose

- Block creating new Pull Requests while allowing normal development flows: push, branch, comment, review, merge, close/reopen.
- Enforce via reverse proxy allowlist and a Git pre-receive hook (to stop AGit `refs/for` PR creation during push).

What this includes

- <nginx/gitea_pr_gate.conf>: Nginx server config that allows only specific state-changing endpoints and blocks PR creation endpoints.
- <hooks/pre-receive-deny-refs-for>: Pre-receive hook to reject pushes to `refs/for/*` (AGit PR flow).
- `policy_server_fastapi.py`: FastAPI-based policy server (recommended) for per-user PR quota.

Requirements

- Python 3.8+ (uses the walrus operator)

Deploy: Nginx (reverse proxy)

- Place <nginx/gitea_pr_gate.conf> in your proxy config, adjust `server_name` and upstream if needed.
- The config:
  - Allows GET/HEAD/OPTIONS everywhere.
  - Allows POST/PATCH for whitelisted endpoints (push via smart HTTP, comments, reviews, merge, close/reopen, etc.).
- Uses `auth_request` to call the quota policy service for PR creation endpoints (Web compare and API /pulls create).
  - Also calls the policy on reopen/close endpoints; the policy only enforces when the PR is currently closed (heuristic for reopen).
  - Handles optional LFS endpoints (comment/remove if not used).

Deploy: Pre-receive hook

- Copy <hooks/pre-receive-deny-refs-for> to Gitea's hooks directory and make it executable:
  - For a global hook (applies to all repos):
    - `sudo install -m 0755 hooks/pre-receive-deny-refs-for /var/lib/gitea/custom/hooks/pre-receive.d/deny-refs-for`
  - Or per-repo: `<repo-path>.git/hooks/pre-receive.d/deny-refs-for`

Notes

- Branch creation is unrestricted by design here; only PR creation is blocked.
- SSH pushes bypass HTTP proxy; the pre-receive hook handles `refs/for` PR creation for both HTTP and SSH.

Per-user PR quota (policy server)
FastAPI server

- Install deps:
  - `pip install fastapi uvicorn httpx structlog prometheus_client`

- Environment variables:
  - `GITEA_BASE_URL` (default `http://127.0.0.1:3000/`)
  - `GITEA_ADMIN_TOKEN` (optional, recommended for private repos; scope: read repository)
  - `PRQ_DEFAULT_MAX` (default `3`)
  - `PRQ_PER_REPO` (optional JSON map `{ "owner/repo": 2 }`)
  - `PRQ_EXEMPT_USERS` (optional comma list `admin,bot`)
  - `PRQ_API_TIMEOUT_SECS` (default `5`)
  - `PRQ_TRUST_PROXY_USER` (default `false`) — if `true`, trust proxy-provided user header (`X-Original-User`) instead of calling Gitea `/api/v1/user`.

- Start:
  - `uvicorn gitea_pr_gate.policy_server_fastapi:app --host 127.0.0.1 --port 9099`
- Observability:
  - Structured JSON logs (structlog) with decision details
  - Prometheus metrics at `/metrics` (scrape the policy server directly)

Behavior

- Receives `auth_request` subrequests at `/validate` with headers:
  - `X-Original-URI`, `Cookie`, `Authorization` (forwarded by Nginx)
- Identifies the calling user via `GET /api/v1/user` using `Cookie`/`Authorization` (default, recommended)
- For PR create endpoints: counts open PRs by that user in the target repo via `GET /api/v1/repos/{owner}/{repo}/issues?type=pulls&state=open&created_by={user}` and `X-Total-Count`
- For reopen/close endpoints: fetches the PR; if current state is `closed`, treats as reopen and enforces the quota; otherwise allows
- Returns 204 (allow) or 403 (deny)

Auth model (documented)

- Policy server determines the actor by calling Gitea `GET /api/v1/user` with the original request's `Cookie` or `Authorization` header.
- Nginx forwards both headers to the internal policy location (see <nginx/gitea_pr_gate.conf>).
- Works for:
  - UI/browser sessions (session cookie)
  - API/CLI calls (`Authorization: token` / `Basic`)
- For private repos, set `GITEA_ADMIN_TOKEN` (read repository) so counting open PRs succeeds.
- Leave `PRQ_TRUST_PROXY_USER=false` unless you explicitly use Reverse Proxy Auth and trust an injected user header.

Nginx integration with policy server

- The config defines:
  - `upstream policy_upstream { server 127.0.0.1:9099; }`
  - Internal location `/_policy/pr_quota` to call the policy server
  - `auth_request /_policy/pr_quota;` on PR-create endpoints
  - If you use Gitea Reverse Proxy Auth, `/_policy/pr_quota` forwards `X-Original-User: $http_x_forwarded_user`, which the FastAPI server can trust when `PRQ_TRUST_PROXY_USER=true`. Ensure your main proxy config strips client-supplied `X-Forwarded-User` and only sets it from your auth layer.

Caveats

- `auth_request` does not pass request body; the quota is enforced for PR creation endpoints only (not for reopen).
- Reopen/close endpoints are gated by a simple heuristic: if the PR is currently `closed`, the request is treated as a reopen attempt and quota is enforced; otherwise it is allowed. This may conservatively block editing a closed PR without reopening if over quota.
- AGit PR creation via `refs/for/*` is blocked by the pre-receive hook (<hooks/pre-receive-deny-refs-for>).

Advanced (optional)

- If you need exact intent detection for reopens, use OpenResty (lua-nginx) or Nginx njs to read request bodies and forward an explicit `desired_state` header to the policy service. Then the policy can gate only when `desired_state=open`.
