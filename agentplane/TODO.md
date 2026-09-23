# agentplane TODO

Entries are removed once landed — this is a burn-down, not a changelog.

## Give Grocy SF its own EgressPolicy instead of an MCP ActionGroup

Grocy's own REST API conventionally authenticates with a static `GROCY-API-KEY` header, and
`cluster/k8s/grocy/sf/mcp/config.yaml` shows the MCP server's own OIDC/proxy wrapping is a
separate concern layered on top of it. **Unconfirmed** — check the `grocy-mcp-oidc-sf`
Secret's fields for the actual Grocy API key before assuming this is a drop-in credential
substitution; the grocy-sf MCP server may do more than pass a bare key through (rate limiting,
response shaping) that would need to move somewhere else first.
