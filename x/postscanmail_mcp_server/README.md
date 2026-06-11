# postscanmail_mcp_server

Hand-authored FastMCP server fronting the [PostScan Mail Developer
API](https://github.com/PostScanMail/api-docs). PostScan Mail does not
publish an OpenAPI spec, so the eleven REST endpoints are wrapped as typed
Python tools in <server.py>.

The server itself holds a single account-wide `x-api-key` and does **no**
per-caller authentication. End-user authentication and the agentydragon-only
ACL come from the `mcp-oauth-facade` sidecar in the same Kubernetes pod —
see <../../cluster/k8s/agents/postscanmail-mcp/app/deployment.yaml> and
the Authentik provider/group/policy block in
<../../tf/gitops/agent-machine-access/main.tf>.

## Tool surface

| Tool                                | Upstream                                                  |
| ----------------------------------- | --------------------------------------------------------- |
| `list_items(sort_order, page)`      | `GET /items`                                              |
| `list_automation_rules(...)`        | `GET /user-defined-rules/system-user-defined-rules`       |
| `set_automation_rule(name, active)` | `PUT /user-defined-rules/update-system-user-defined-rule` |
| `request_open(addr, ids)`           | `POST /addresses/{addr}/items/actions/open` (paid)        |
| `cancel_open(addr, ids)`            | `POST /addresses/{addr}/items/actions/open/cancel`        |
| `request_discard(addr, ids)`        | `POST /addresses/{addr}/items/actions/discard`            |
| `cancel_discard(addr, ids)`         | `POST /addresses/{addr}/items/actions/discard/cancel`     |
| `request_rescan(addr, ids)`         | `POST /addresses/{addr}/items/actions/rescan` (paid)      |
| `cancel_rescan(addr, ids)`          | `POST /addresses/{addr}/items/actions/rescan/cancel`      |
| `request_shred(addr, ids)`          | `POST /addresses/{addr}/items/actions/shred`              |
| `cancel_shred(addr, ids)`           | `POST /addresses/{addr}/items/actions/shred/cancel`       |

Base URL: `https://api.postscanmail.com/api/account-docs/v2/`.

## Running locally

```bash
POSTSCANMAIL_API_KEY=... bb run //x/postscanmail_mcp_server:server -- --help
curl -X POST http://localhost:8080/mcp \
  -H 'Accept: application/json,text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

## When PostScan Mail ships an OpenAPI spec

Switch to `FastMCP.from_openapi` (the grocy_mcp pattern in
<../grocy_mcp/server.py>) and delete the hand-rolled tool wrappers.
