"""The Action groups and OAuth-linked MCP server the MCP suites exercise. The k3s fixture configures
them in place of the deployment's own, so the scenario never depends on a name an operator uses."""

# `mcp-everything`, which needs no credential.
EVERYTHING_GROUP = "test-everything"
# The OAuth-protected MCP fixture: the server the operator links through Dex, and the group that calls
# it, which takes its server's name.
OAUTH = "test-oauth"
# Both servers' tool, which answers `Echo: <message>`.
ECHO = "echo"
