# haku/console/frontend TODO

- Unify the action/identity header across registered and unregistered tools. A widget's
  `ActionBadge` (e.g. grocy's "Remove stock") largely restates the `serverId.toolName` the card
  header already shows. Want one treatment: a registered tool draws its own custom identity
  header; an unregistered one renders `serverId.toolName` in the _same_ styling — so the header
  reads consistently either way instead of the widget duplicating the tool name.

- Let the pending tool-call note (`pending_tool_call_actions.tsx`) ride an **approve**, not just
  a deny — a general operator remark, not only a denial reason. The agent can already read
  decision notes back from the tool-call result DB, so this is mostly: persist a reason on the
  approve path of the decision endpoint (`mcp_approval.py`) and give the field a neutral
  placeholder when it applies to both outcomes.
