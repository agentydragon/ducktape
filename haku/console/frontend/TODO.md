# haku/console/frontend TODO

- Let the pending tool-call note (`pending_tool_call_actions.tsx`) ride an **approve**, not just
  a deny — a general operator remark, not only a denial reason. The agent can already read
  decision notes back from the tool-call result DB, so this is mostly: persist a reason on the
  approve path of the decision endpoint (`mcp_approval.py`) and give the field a neutral
  placeholder when it applies to both outcomes.

- Preview widget for **`tana-rw` › `import_tana_paste`** (`tool_previews/`): render the Tana
  Paste body (compact = first lines, detailed = full) plus a link to the parent/target node.
  Blocked on the arg schema — `tana-rw` is a **remote** server (`tana-mcp-facade.allegedly.works`,
  the `ghcr.io/agentydragon/tana-desktop` image wrapping Tana desktop's own MCP), so the shape
  isn't in the console's Pydantic models and `tools/list` is OAuth-gated (can't fetch headless).
  To do it: connect the `tana-rw` MCP account in the console, read the reflected input schema from
  `GET /api/capabilities/mcp-servers` (or the desktop MCP's `tools/list`), hand-write the matching
  zod schema (the other remote-server widgets like `kubectl` do this rather than using
  `schema.zod.ts`), and confirm the Tana node deep-link URL form for the parent-node link.
