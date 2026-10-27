# Action Service test fixtures

## MCP fixture selection

| Boundary under test                                       | Fixture                                                                                                                                                                                                                             |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Action Service startup, upstream MCP recovery and linkage | `//agentplane/action_service:integration_test`, including its local upstream MCP peer in [main_test.go](../main_test.go)                                                                                                            |
| External MCP/OAuth serving and edge composition           | `//agentplane/mcp_edge:runtime_test`, `//agentplane/mcp_edge/authserver:integration_test`, `//agentplane/mcp_edge/connections:integration_test`, and `//agentplane/mcp_edge/mcpserver:mcpserver_test`                               |
| Actions exercised by real harnesses                       | The upstream Everything image, pinned by `MODULE.bazel`'s `mcp_everything` pull and configured in [mcp_fixture_servers.py](../../system_tests/k3s/mcp_fixture_servers.py), used by [MCP acceptance](../../system_tests/test_mcp.py) |
| MCP OAuth linkage through the app callback                | `build_dex_app()` in [oauth_mcp_server.py](oauth_mcp_server.py), verifying Dex-issued JWTs; Everything has no authentication                                                                                                        |

The system tests create scoped PolicySets and bindings for its Sandbox; running Everything
alone does not auto-approve its tools. Both fixtures run only on the
[k3s system test stack](../../system_tests/k3s/README.md), cluster-internal and with no workload credentials.

The OAuth system test scenario reuses the operator's Dex login, completes authorization through the
app callback, then executes the protected echo tool. The fixture verifies tokens with Dex's JWKS;
the MCP edge owns external OAuth, while the Action Service retains credentials only for configured
upstream MCP linkage. This fixture exercises external authorization-server linkage rather than
duplicating Everything's unauthenticated tool catalog.
