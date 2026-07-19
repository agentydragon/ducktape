# MCP Tool Annotations (`readOnlyHint`, etc.)

MCP lets a tool declare what _kind_ of operation it is via optional
`annotations` on the tool definition (MCP spec `2025-06-18`, type
`mcp.types.ToolAnnotations`). Clients read these to group tools and to relax
approval prompts — claude.ai / Claude Code, for example, treat a tool as
**read-only** iff the server sets `annotations.readOnlyHint: true`, and auto-run
such tools where a mutating tool would prompt.

All fields are **hints**: the spec says they are not guaranteed faithful and
clients must not gate security decisions on hints from untrusted servers. They
are a UX / convenience signal, not a permission boundary.

## Fields and defaults

| Field             | Default | Meaning                                                                              |
| ----------------- | ------- | ------------------------------------------------------------------------------------ |
| `readOnlyHint`    | `false` | Tool does not modify its environment.                                                |
| `destructiveHint` | `true`  | May perform destructive updates (only meaningful when `readOnlyHint == false`).      |
| `idempotentHint`  | `false` | Repeating the call with the same args has no extra effect (`readOnlyHint == false`). |
| `openWorldHint`   | `true`  | Interacts with an open world (web search) vs a closed one (memory).                  |
| `title`           | —       | Human-readable display title.                                                        |

Because `readOnlyHint` defaults to `false`, unannotated tools are assumed
mutating — which is why the read-only/other split looks binary in practice:
most servers never bother annotating, so they all fall into "other".

## Setting them in this repo

Import the type and pass `annotations=` at registration:

```python
from mcp.types import ToolAnnotations


@mcp.tool(  # standard FastMCP decorator
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True),
)
async def search_nodes(query: str) -> str: ...
```

The flat-model path is already plumbed — `@flat_model` forwards `annotations`
into the underlying `Tool(...)` (`mcp_infra/enhanced/flat_mixin.py`):

```python
@enhanced.flat_model(
    "search_nodes",
    annotations=ToolAnnotations(readOnlyHint=True),
)
def search_nodes(payload: SearchRequest) -> SearchResult: ...
```

## Why set them

Read-only servers (e.g. grocy read-only paths, OSM lookup, anything that only fetches) should
declare `readOnlyHint=True` so clients stop gating them behind per-call approvals. Mutating tools
can use `destructiveHint=False` / `idempotentHint=True` to qualify how they change state.

Currently annotated: haku-console's own read meta-tools (`list_mcp_servers`, `get_tool_call`,
`list_tool_calls` — closed-world, so also `openWorldHint=False`) and the in-process `gmail` and
`google_calendar` read tools (`readOnlyHint=True`; external mailbox/calendar, so `openWorldHint`
left at its default `true`). `flat_mixin.py` and `google_discovery.py` (`GenTool.annotations`)
both forward the type to their generated tools.

## Propagation through haku-console

haku-console re-exposes connected-server tools as `<server>_<tool>` proxies. The proxy propagates
the upstream tool's `annotations` **unchanged** (`ToolMetadata.annotations` → `ProxyTool`), so
annotating any upstream tool (in-process or remote) reaches the client as it would on a direct
connection. The hint is advisory and does not affect the console's server-side approval policy.
