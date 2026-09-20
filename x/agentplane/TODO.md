# x/agentplane TODO

Entries are removed once landed — this is a burn-down, not a changelog.

## Give PostScanMail its own EgressPolicy instead of an MCP ActionGroup

PostScanMail's Developer API (`x/postscanmail_mcp_server`, `BASE_URL =
"https://api.postscanmail.com/api/account-docs/v2"`) authenticates with a single static
`x-api-key` header (`server.py`'s `build_client`) — no OAuth, no per-call negotiation. There is
no reason a sandbox needs an MCP server standing between it and that API: an `EgressPolicy` +
`EgressCredential` (`source: secret_ref`, `targets: [{header: "x-api-key", method:
"wholeValue"}]`) would let a sandbox call `api.postscanmail.com` directly with a placeholder
key, the same way Forgejo's password and the Kubernetes bearer are already substituted
(`x/agentplane/docs/sandbox_actions.md`). Removes a whole MCP round-trip and a maintained
executor for one static credential.

Not wired as an ActionGroup here (deliberately deferred, 2026-09-20): the console's existing
`postscanmail_mcp` server is OAuth-wrapped for its own reasons (dynamic client registration
through the shared mcp-oauth-facade, restricted to the operator by Authentik group policy —
`tf/gitops/agent-machine-access/postscanmail-mcp.tf`); check whether that consent boundary
still needs to exist once the caller is a labeled ServiceAccount admitted by an
`ActionPolicyBinding` rather than an arbitrary OAuth client.

## Give Grocy SF its own EgressPolicy instead of an MCP ActionGroup

Same shape as PostScanMail, likely: Grocy's own REST API conventionally authenticates with a
static `GROCY-API-KEY` header, and `cluster/k8s/grocy/sf/mcp/config.yaml` shows the MCP
server's own OIDC/proxy wrapping is a separate concern layered on top of it. **Unconfirmed** —
check the `grocy-mcp-oidc-sf` Secret's fields for the actual Grocy API key before assuming this
is a drop-in credential substitution; the grocy-sf MCP server may do more than pass a bare key
through (rate limiting, response shaping) that would need to move somewhere else first.
