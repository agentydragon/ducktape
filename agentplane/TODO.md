# agentplane TODO

Entries are removed once landed — this is a burn-down, not a changelog.

## Give Grocy SF its own EgressPolicy instead of an MCP ActionGroup

Grocy's own REST API conventionally authenticates with a static `GROCY-API-KEY` header, and
`cluster/k8s/grocy/sf/mcp/config.yaml` shows the MCP server's own OIDC/proxy wrapping is a
separate concern layered on top of it. **Unconfirmed** — check the `grocy-mcp-oidc-sf`
Secret's fields for the actual Grocy API key before assuming this is a drop-in credential
substitution; the grocy-sf MCP server may do more than pass a bare key through (rate limiting,
response shaping) that would need to move somewhere else first.

## Drop `conversation_payload_manifest.present`

The column is written `True` on every manifest row and read by nobody: the payload interest
response carried it to the browser, which never looked at it, and that route is gone. Removing
it is an alembic migration plus the write in `_write_conversation_payloads`, kept out of the
window-content change so that change stayed about the shapes.
