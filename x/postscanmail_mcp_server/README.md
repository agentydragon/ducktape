# postscanmail_mcp_server

Hand-authored FastMCP server fronting the [PostScan Mail Developer
API](https://github.com/PostScanMail/api-docs). PostScan Mail publishes no
OpenAPI/Swagger spec — the docs repo is markdown-only and the live API serves
neither `/openapi.json` nor `/swagger.json` — so the eleven REST endpoints are
wrapped as typed Python tools in <server.py>. Each tool's docstring links its
upstream endpoint doc; the two reads return typed Pydantic models (see
[Response schemas](#response-schemas)), and mutating/action tools return the
upstream JSON verbatim.

The server itself holds a single account-wide `x-api-key` and does **no**
per-caller authentication. Its Kubernetes app is decommissioned and its manifests
are parked at <../../cluster/k8s/parked/postscanmail-mcp/>. The former deployment
used an `mcp-oauth-facade` sidecar and a Terraform-managed Authentik client. The
OAuth client is being retired; this source is not currently registered with
haku-console or exposed through a deployed OAuth facade. Any revival must restore
an authentication boundary before exposing the server.

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

Reads return typed models (`MailItemsPage`, `AutomationRulesPage`); mutating/action tools
return the upstream JSON unchanged (`object`) — PostScan Mail documents no response shape
for them. Every tool's docstring links its upstream endpoint doc.

## Response schemas

Built from observed payloads (PostScan Mail documents no shapes — "responses vary depending
on account data"). Both reads share a Laravel `LengthAwarePaginator` envelope: `current_page`,
`last_page`, `per_page`, `total`, `next_page_url`, `prev_page_url`.

- `list_items` → `MailItemsPage` with `items: list[MailItem]`. Each `MailItem` carries
  `mail_id`, `sender_name`, `address_id`, `ai_summary` (PostScan Mail's own per-piece summary,
  `list[str]`), signed `cover_image`/`pdf_content` URLs (absent until opened/scanned — this
  tool lists them, it does not download the content), and `pdf_metadata` (`received_at`,
  `current_status`, `current_folder_name`, `uploaded_from_address`).
- `list_automation_rules` → `AutomationRulesPage` with `rules: list[AutomationRule]`
  (`auto_scan`/`auto_shred`/`auto_discard`/`auto_ai_summary` booleans, per user).

## Tool annotations

Each tool declares [`ToolAnnotations`](../../mcp_infra/docs/tool_annotations.md) so MCP
clients (claude.ai / Claude Code) can group it and relax approval prompts. The hints are
advisory and are not a security boundary; an integrating server must enforce its own
approval policy.

| Tool(s)                               | Annotations                                                    |
| ------------------------------------- | -------------------------------------------------------------- |
| `list_items`, `list_automation_rules` | `readOnlyHint=true` (pure GET reads)                           |
| `set_automation_rule`                 | `idempotentHint=true, destructiveHint=false` (PUT toggle)      |
| `cancel_open/discard/rescan/shred`    | `idempotentHint=true, destructiveHint=false` (abort a pending) |
| `request_open`, `request_rescan`      | `destructiveHint=false` (paid, additive scan)                  |
| `request_discard`, `request_shred`    | `destructiveHint=true` (remove/destroy mail)                   |

## Deployment status

The PostScanMail MCP app is decommissioned. The server source and tests remain here for
possible revival; haku-console does not currently register this server. The tool
annotations below describe intended client behavior, not an active approval boundary.

## Running locally

```bash
POSTSCANMAIL_API_KEY=... bb run //x/postscanmail_mcp_server:server -- --help
curl -X POST http://localhost:8080/mcp \
  -H 'Accept: application/json,text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

## When PostScan Mail ships an OpenAPI spec

No spec exists today (verified against the docs repo and the live API). If one ships, switch
to `FastMCP.from_openapi` (the grocy_mcp pattern in <../grocy_mcp/server.py>) and delete the
hand-rolled tool wrappers and response models.
