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
  ├── /mcp  — unified MCP endpoint
  │           x-authentik-jwt → operator browser (JWT-verified)
  │           Authorization: Bearer → OpenClaw agent (AGENT_API_KEY)
  ├── /api  — operator REST API (Authentik JWT auth)
  └── /     — operator Svelte SPA (Authentik SSO, JWT-verified)
        │  MCP ResourceUpdated: resource://actions/{id}
        ▼
OpenClaw plugin (openclaw/approval-gate/)
  ├── re-registers approval gate tools for the OpenClaw agent
  └── translates ResourceUpdated → chat.inject gateway RPC
```

`CiliumNetworkPolicy` allows both the Authentik outpost and OpenClaw pods to reach
port 8765. Access to `/mcp` is controlled by auth header: JWT for the operator
browser, `AGENT_API_KEY` bearer for the OpenClaw plugin.

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
| `config.py`         | `Settings` (Pydantic); backend spec from YAML, auth keys from env      |
| `proxy_server.py`   | `ApprovalGateServer` — core MCP proxy, tool wrapping, notifications    |
| `operator_api.py`   | FastAPI operator REST router (`/api/actions/*`)                        |
| `ui.py`             | Serves the Svelte SPA `index.html` for operator routes                 |
| `app.py`            | Combined FastAPI app factory (`create_app()`)                          |
| `main.py`           | `uvicorn` entry point (single server, single port)                     |
| `instructions.mako` | Mako template for MCP `initialize` instructions                        |
| `frontend/`         | Svelte 5 operator SPA (action list + detail, approve/reject workflow)  |

## Configuration

### Backend specs (YAML config file)

Backend MCP servers are configured via a YAML file. Set `CONFIG_PATH` to its path
(default: `/etc/approval-gate/config.yaml`). Each backend is keyed by its namespace
prefix (lowercase alphanumeric + underscore).

```yaml
backends:
  exec:
    url: http://exec-backend:8766/mcp
    headers:
      Authorization: "Bearer token123"
  files:
    command: /usr/bin/file-server
    args:
      - --mcp
```

Each backend entry supports the full `MCPServerTypes` config (URL + headers for
streamable-http, command + args + env for stdio).

### Environment variables

| Variable            | Required | Description                                                                                   |
| ------------------- | -------- | --------------------------------------------------------------------------------------------- |
| `AGENT_API_KEY`     | yes      | Bearer token for the agent-facing `/mcp` endpoint                                             |
| `PUBLIC_BASE_URL`   | yes      | Base URL for approval links shown to agents                                                   |
| `OPERATOR_JWKS_URL` | yes      | JWKS endpoint for verifying operator UI JWTs (e.g. Authentik's `/application/o/<slug>/jwks/`) |
| `CONFIG_PATH`       | no       | Path to YAML config file (default `/etc/approval-gate/config.yaml`)                           |
| `DB_PATH`           | no       | SQLite DB path (default `/data/approval_gate.db`)                                             |
| `PREDICATE_PATH`    | no       | Path to Python predicate file (default: always queue for human)                               |
| `HOST`              | no       | Server bind address (default `0.0.0.0`)                                                       |
| `PORT`              | no       | Server port (default `8765`)                                                                  |

## Predicate file format

```python
from approval_gate.predicates import Approved, Denied, NeedsHumanDecision

def decide(server_namespace: str, tool_name: str, arguments: dict) -> Approved | Denied | NeedsHumanDecision:
    if server_namespace == "exec" and tool_name == "run_command":
        return Approved()
    return NeedsHumanDecision()  # default: always queue for operator
```

Fail-safe: any exception during predicate evaluation defaults to `NeedsHumanDecision`.
