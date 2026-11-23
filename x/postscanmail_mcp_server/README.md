# PostScan Mail MCP server

PostScan Mail documents its endpoints in Markdown. The list tools return typed
pages based on observed responses. The other tools return the upstream JSON. [Developer API](https://github.com/PostScanMail/api-docs) · [Tool definitions](server.py)

The server uses one account-wide `x-api-key`. Its former deployment used an
`mcp-oauth-facade` sidecar to restrict callers to `agentydragon`. The app is
decommissioned and haku-console does not register it. Restore per-caller
authentication and access control before exposing it again.
[Parked deployment](../../cluster/parked/postscanmail-mcp/deployment.yaml)

## Observed response behavior

`list_items` includes PostScan Mail's own `ai_summary` and signed
`cover_image`/`pdf_content` URLs when a piece has been opened or scanned. The
tool lists these URLs; it does not download the content.

Tool annotations are client hints that may affect approval prompts, not a
security boundary. Integrating servers must enforce their own approval policy;
see [tool annotation guidance](../../mcp_infra/docs/tool_annotations.md).

## Running locally

Set `POSTSCANMAIL_API_KEY` to the account-wide API key, then run:

```bash
bb run //x/postscanmail_mcp_server:server_cli
```

The default endpoint is `http://localhost:8080/mcp`.

## OpenAPI follow-up

The original integration found no spec at `/openapi.json` or `/swagger.json`.
If PostScan Mail publishes one, consider `FastMCP.from_openapi`
(see the [grocy MCP server](../../grocy_mcp/server.py)) and remove the hand-written
wrappers and observed response models.
