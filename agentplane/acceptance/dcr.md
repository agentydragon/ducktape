# External-client dynamic registration acceptance

`//agentplane/acceptance:test_dcr_k3s` starts an actual MCP Python SDK `ClientSession` over
Streamable HTTP against the Action Service of a k3s stack with the MCP fixtures, at the `/mcp`
endpoint of the loopback origin its OAuth server names itself by (`ACTIONS_MCP_URL`, a port-forward:
[`x/k3s_env`](../x/k3s_env/README.md)). The SDK receives the unauthenticated challenge, fetches its
protected-resource metadata, follows the advertised authorization server, and registers through its
advertised endpoint. Wire observers require JSON content types and RFC 7591 `201`; the SDK validates
the result, and the probe checks that the requested client metadata survives. Loopback and
hosted-style HTTPS redirect URIs exercise both client shapes.

This is **registration-only acceptance**, not completed OAuth or Claude.ai compatibility proof. It
deliberately stops at the SDK redirect callback, before fetching `/authorize`, reading operator
credentials, exchanging tokens, or completing authenticated MCP initialization; the fixture's Dex has
no client for the Action Service's OAuth server. No Action, sandbox, model turn, or operator grant is
created. HTTP logging is suppressed; SDK exceptions are replaced with phase-only diagnostics and
`--showlocals` is refused. Requests have 15-second timeouts and each scenario has a 60-second overall
bound. The registrations go with the stack.

`test_real_sdk_dcr_over_http_persists_client_metadata` in `action_service:test_oauth` runs the same
probe over real TCP against the Actions app with PostgreSQL and the existing hermetic upstream IdP.
It checks metadata through a separately constructed provider and verifies that SDK-generated
authorization reaches the consent boundary without creating caller authority. This regression
coverage is not evidence that a deployed Action Service's DCR succeeded.
