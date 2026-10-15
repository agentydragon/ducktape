# Action Service test fixtures

## MCP fixture selection

| Boundary under test                                                | Fixture                                                                                                                                                                                                      |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Catalog, policy and execution composition without a deployed image | `echo_mcp_url` in [conftest.py](../conftest.py), an in-process FastMCP server used by [test_runtime.py](../test_runtime.py)                                                                                  |
| Deployed Actions exercised by real harnesses                       | The upstream Everything image, pinned and configured in [actions_testing_fixtures.py](../../../cluster/cdk8s/agentplane/actions_testing_fixtures.py), used by [MCP acceptance](../../acceptance/test_mcp.py) |
| Hermetic MCP OAuth linkage                                         | `build_app()` in [oauth_mcp_server.py](oauth_mcp_server.py), using an in-memory OAuth provider                                                                                                               |
| Deployed MCP OAuth linkage through the app callback                | `build_dex_app()` in the same module, verifying Dex-issued JWTs; Everything has no authentication                                                                                                            |

The acceptance suite creates scoped PolicySets and bindings for its Sandbox; deploying Everything
alone does not auto-approve its tools. The deployed fixtures are cluster-internal. Everything has no
workload credentials or outbound network access; its image and isolation settings are owned by the
[deployment construct](../../../cluster/cdk8s/agentplane/actions_testing_fixtures.py).

The OAuth acceptance scenario reuses the operator's Dex login, completes authorization through the
app callback, then executes the protected echo tool. The fixture verifies tokens with Dex's JWKS;
the Action Service holds its OAuth client credentials. This fixture exists to exercise external
authorization-server linkage, rather than to duplicate Everything's unauthenticated tool catalog.
