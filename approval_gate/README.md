# approval_gate

Generic MCP approval proxy. Wraps one or more backend MCP servers and adds a human-in-the-loop
approval layer to every tool call. Agents queue actions; operators approve/reject via
a Svelte UI; notifications flow back via MCP `ResourceUpdated` events.

Each backend is mounted under a namespace prefix — tools are exposed as `{namespace}_{tool}`
(e.g., backend `exec` with tool `run_command` becomes `exec_run_command`).

## Architecture

```
Backend MCP servers (streamable-http or stdio)
  exec ──┐
  files ─┤  mounted under namespace prefixes
  ...  ──┘
         ▼
ApprovalGateServer (FastMCP proxy)        port 8765
  ├── /mcp          — unified MCP endpoint (Authorization: Bearer <JWT>)
  ├── /auth/config  — OIDC config for SPA (authority, client_id, redirect_uri)
  └── /             — operator Svelte SPA (OAuth2 PKCE flow)
         ▲
         │  MCP over HTTP (JWT-authenticated)
         │
auth-proxy sidecar (openclaw-gateway pod) port 8767
  ├── accepts unauthenticated MCP from OpenClaw plugin
  ├── injects OAuth2 Bearer token (client_credentials grant from Authentik)
  └── forwards authenticated MCP to upstream approval gate
         ▲
         │  MCP over HTTP (unauthenticated, localhost only)
         │
OpenClaw plugin (approval-gate entry in plugins.entries)
  └── connects to approval gate via auth-proxy sidecar (localhost:8767)
```

All tokens are JWTs verified against the JWKS endpoint discovered from the
OIDC issuer's `.well-known/openid-configuration` (Authentik).
Operator tokens are obtained by the SPA via OAuth2 Authorization Code + PKCE;
agent tokens arrive via `Authorization: Bearer` (client_credentials flow
through the auth-proxy sidecar). Both use `Authorization: Bearer` headers.
JWT scopes determine capabilities: `propose` (agent), `decide` (operator),
`read` (both).

## Running

```bash
bazel run //approval_gate:server
```

Required env vars must be set (see below). `CONFIG_PATH` must point to a YAML file
with the backend spec (see below).

## Key modules

| Module              | Purpose                                                                |
| ------------------- | ---------------------------------------------------------------------- |
| `models.py`         | Discriminated union types (`Action`, `ActionState`, `ToolCallOutcome`) |
| `storage.py`        | aiosqlite CRUD; indexed `status` column                                |
| `predicates.py`     | Three-way predicate: `Approved \| Denied \| NeedsHumanDecision`        |
| `config.py`         | `Settings` (Pydantic); backend spec + auth config from YAML            |
| `proxy_server.py`   | `ApprovalGateServer` — core MCP proxy, tool wrapping, scope constants  |
| `app.py`            | Starlette app factory (`create_app()`) + uvicorn entry point           |
| `instructions.mako` | Mako template for MCP `initialize` instructions                        |
| `auth_proxy/`       | OAuth2 sidecar: FastMCP proxy with client_credentials token injection  |
| `frontend/`         | Svelte 5 operator SPA (action list + detail, approve/reject workflow)  |

## Configuration

### Backend specs (YAML config file)

Backend MCP servers are configured via a YAML file. Set `CONFIG_PATH` to its path
(default: `/etc/approval-gate/config.yaml`). Each backend is keyed by its namespace
prefix (lowercase alphanumeric + underscore).

```yaml
backends:
  kubeapi_admin:
    url: http://kubeapi-admin-exec-mcp:8766/mcp
  files:
    command: /usr/bin/file-server
    args:
      - --mcp
```

Each backend entry supports the full `MCPServerTypes` config (URL + headers for
streamable-http, command + args + env for stdio).

### Environment variables

| Variable      | Required | Description                                                         |
| ------------- | -------- | ------------------------------------------------------------------- |
| `CONFIG_PATH` | no       | Path to YAML config file (default `/etc/approval-gate/config.yaml`) |

All other settings (`public_base_url`, `oidc_issuer`, `oidc_client_id`, `backends`,
`db_path`, `predicate_path`, `host`, `port`) are loaded from the YAML config file.

## Predicate file format

```python
from approval_gate.predicates import Approved, Denied, NeedsHumanDecision

def decide(server_namespace: str, tool_name: str, arguments: dict) -> Approved | Denied | NeedsHumanDecision:
    if server_namespace == "exec" and tool_name == "run_command":
        return Approved()
    return NeedsHumanDecision()  # default: always queue for operator
```

Fail-safe: any exception during predicate evaluation defaults to `NeedsHumanDecision`.
