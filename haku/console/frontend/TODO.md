# haku/console/frontend TODO

- Let the pending tool-call note (`pending_tool_call_actions.tsx`) ride an **approve**, not just
  a deny — a general operator remark, not only a denial reason. The agent can already read
  decision notes back from the tool-call result DB, so this is mostly: persist a reason on the
  approve path of the decision endpoint (`mcp_approval.py`) and give the field a neutral
  placeholder when it applies to both outcomes.

- Add previews for the remaining **`tana-rw`** tools: `list_workspaces`, `search_nodes`, `read_node`,
  `get_children`, `list_tags`, `get_tag_schema`, `tag`, `set_field_option`, `set_field_content`,
  `create_tag`, `add_field_to_tag`, `set_tag_checkbox`, `check_node`, `uncheck_node`, and `open_node`.
  The generic JSON preview remains intentional until each has a useful operator-focused rendering.
