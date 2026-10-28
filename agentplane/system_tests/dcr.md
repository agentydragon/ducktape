# External-client dynamic registration

`//agentplane/mcp_edge/authserver:sdk_dcr_test` starts the production Go MCP and OAuth handlers
with real PostgreSQL, then runs the actual MCP Python SDK `ClientSession` over Streamable HTTP.
The SDK follows the unauthenticated challenge through protected-resource and authorization-server
metadata to the advertised registration endpoint. The unchanged [wire helper](../mcp_edge/authserver/dcr.py)
checks JSON content types, RFC 7591 `201`, one registration, persisted public-client metadata,
the authorization URL, PKCE/state, and absence of a token. Loopback and hosted-style HTTPS
redirect URIs both run. The existing auth-server fixture supplies upstream OIDC discovery;
the registration path never signs in there.

`//agentplane/system_tests:test_dcr` independently exercises the deployed edge image and generated
configuration through the k3s Service/port-forward (`MCP_EDGE_URL`; [`k3s`](k3s/README.md)). Plain HTTP
follows the real 401 challenge and metadata chain, checks same-origin endpoints, and registers a
public loopback client. It does not depend on the SDK helper, so ordinary helper-only changes
select the component test without starting k3s. Changes affecting the edge deployment and broad
CI triggers still select the system smoke. A full sweep runs both targets.

Both prove **registration only**, not completed OAuth or Claude.ai compatibility. The SDK stops at
its redirect callback; the smoke stops at the registration response. Neither fetches `/authorize`,
reads operator credentials, exchanges tokens, completes authenticated MCP initialization, or creates
an Action, Sandbox, model turn, or operator grant. HTTP/OAuth failures have sanitized diagnostics;
the pytest smoke refuses `--showlocals`. Requests have 15-second timeouts and each registration
scenario has a 60-second overall bound. Registrations go with their disposable databases.
