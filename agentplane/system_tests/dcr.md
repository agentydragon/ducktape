# External-client dynamic registration

`//agentplane/system_tests:test_dcr` starts an actual MCP Python SDK `ClientSession` over
Streamable HTTP against the standalone MCP edge of a k3s stack with the MCP fixtures, at the `/mcp`
endpoint of the loopback origin its OAuth server names itself by (`MCP_EDGE_URL`, a port-forward:
[`k3s`](k3s/README.md)). The SDK receives the unauthenticated challenge, fetches its
protected-resource metadata, follows the advertised authorization server, and registers through its
advertised endpoint. Wire observers require JSON content types and RFC 7591 `201`; the SDK validates
the result, and the probe checks that the requested client metadata survives. Loopback and
hosted-style HTTPS redirect URIs exercise both client shapes.

This proves **registration only**, not completed OAuth or Claude.ai compatibility proof. It
deliberately stops at the SDK redirect callback, before fetching `/authorize`, reading operator
credentials, exchanging tokens, or completing authenticated MCP initialization. The fixture has a
Dex client for the edge's upstream OAuth callback, but this scenario does not exercise it. No Action,
sandbox, model turn, or operator grant is
created. HTTP logging is suppressed; SDK exceptions are replaced with phase-only diagnostics and
`--showlocals` is refused. Requests have 15-second timeouts and each scenario has a 60-second overall
bound. The registrations go with the stack.
