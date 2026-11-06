# PostScan Mail MCP server

Hand-written FastMCP wrappers for the PostScan Mail API. The API has no published
OpenAPI spec, so read results use models based on observed payloads; mutating
and action tools return opaque upstream JSON. See [server.py](server.py) for the
current tools, parameters, and endpoint documentation.

The server uses one account-wide `x-api-key` and has no per-caller
authentication. The app is decommissioned, its manifests are parked at
<../../cluster/parked/postscanmail-mcp/>, and haku-console does not register this
server. Any revival must restore an authentication boundary before exposure.

## Observed response behavior

`list_items` includes PostScan Mail's own `ai_summary` and signed
`cover_image`/`pdf_content` URLs when a piece has been opened or scanned. The
tool lists these URLs; it does not download the content. The read models reflect
observed payloads because PostScan Mail documents no response schemas.

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

If PostScan Mail publishes an OpenAPI spec, consider `FastMCP.from_openapi`
(see the [grocy MCP server](../../grocy_mcp/server.py)) and remove the hand-written
wrappers and observed response models.
