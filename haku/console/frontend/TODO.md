# haku/console/frontend TODO

- Let the pending tool-call note (`pending_tool_call_actions.tsx`) ride an **approve**, not just
  a deny — a general operator remark, not only a denial reason. The agent can already read
  decision notes back from the tool-call result DB, so this is mostly: persist a reason on the
  approve path of the decision endpoint (`mcp_approval.py`) and give the field a neutral
  placeholder when it applies to both outcomes.

- Add previews for the remaining **`tana-rw`** tools: `list_workspaces`, `search_nodes`, `read_node`,
  `get_children`, `list_tags`, `get_tag_schema`, `tag`, `set_field_content`, `create_tag`,
  `add_field_to_tag`, `set_tag_checkbox`, `check_node`, `uncheck_node`, and `open_node`.
  The generic JSON preview remains intentional until each has a useful operator-focused rendering.

- Give the CLI's own **`Bash`** tool a real rendering in the conversation view. It reaches the
  transcript as a harness tool rather than through an MCP server, so `x/tool_call.tsx` renders it —
  the same JSON blob and code block every tool gets — while `tool_rendering/hostexec/` already shows
  what a `bash` widget is worth: the command as a command, a discriminated exit status, stdout as
  output rather than as a quoted string. The widget largely exists; what does not is any path from
  the conversation view to it.

- Render the runner's calls **to haku-console** with the widgets those tools already have, instead
  of the generic JSON fallback. Every one of them is an MCP tool `tool_rendering/` knows —
  `toolPreview` and friends dispatch on the MCP server id — but a transcript row cannot reach that
  key. Three separate gaps, and each has to close for the dispatch to be sound:
  - `ToolCallItem` (<../conversation/item_reads.py>) carries `tool_name` and `arguments` and no
    `server_id`, which is what `tool_rendering/index.tsx` indexes by.
  - The proxy's name is not one split away from that key. `server_tool_prefix`
    (<../mcp_config.py>) sanitizes the id into the tool namespace — `grocy-sf` and `tana-rw` become
    `grocy_sf` and `tana_rw` — while `server_ids.ts` holds the verbatim config ids, and the mapping
    is not invertible. It has to come from the server catalog, not from parsing the name.
  - An approval-gated call's recorded arguments are the `{input, rationale}` envelope, because the
    transcript is a fold of what the CLI sent and `mcp_server.py` unwraps the envelope
    server-side — so `/api/tool-calls` rows, which is what the approvals and history surfaces feed
    the widgets, are already unwrapped and transcript rows are not.

  `call_mcp_tool` is the easy half: `server_id` is one of its arguments.
