# External-client dynamic registration acceptance

`//x/agentplane/acceptance:test_dcr` starts an actual MCP Python SDK `ClientSession`
over Streamable HTTP against `https://agentplane-actions-testing.allegedly.works/mcp`.
The SDK receives the unauthenticated challenge, fetches its protected-resource metadata,
follows the advertised authorization server, and registers through its advertised endpoint.
Wire observers require JSON content types and RFC 7591 `201`; the SDK validates the result,
and the probe checks that the requested client metadata survives. Loopback and hosted-style
HTTPS redirect URIs exercise both client shapes. This target has no staging override.

This is **registration-only acceptance**, not completed OAuth or Claude.ai compatibility
proof. It deliberately stops at the SDK redirect callback, before fetching `/authorize`,
reading operator credentials, exchanging tokens, or completing authenticated MCP initialization.
No Action, sandbox, model turn, or operator grant is created. HTTP logging is suppressed;
SDK exceptions are replaced with phase-only diagnostics and `--showlocals` is refused.
Requests have 15-second timeouts and each scenario has a 60-second overall bound.

Run explicitly from the authorized controlled host (not an agent pod or RBE):

```bash
bazelisk test //x/agentplane/acceptance:test_dcr --nocache_test_results --test_output=errors
```

Missing OAuth deployment configuration is a **failure**, never a skip. The testing Actions
Deployment currently lacks `AGENTPLANE_ACTIONS_OAUTH`, and testing Dex currently registers
only the integration app's callback. The existing Dex app-login helper therefore is not yet
an external-MCP login/consent harness. Full OAuth acceptance needs a testing Actions upstream
client/callback, signing/encryption keys, and the matching operator/subject mapping before
reusing legitimate Dex login and the app's consent API. Do not substitute workload tokens,
insert sessions/grants, or use staging human credentials to make this target pass.

Each scenario uses a fresh in-memory SDK store and unique client name. FastMCP 3.4.4 does
not expose RFC 7592 registration management or a client deletion endpoint, and its server
registration store has no TTL: successful live runs leave two inert registration records.
The probe clears its local store but does not claim to delete server records. Keep runs
explicit and bounded; administrative test-registration cleanup needs a separately authorized
server facility, not direct database deletion or abuse of token revocation.

`test_real_sdk_dcr_over_http_persists_client_metadata` in `action_service:test_oauth` runs
the same probe over real TCP against the Actions app with PostgreSQL and the existing hermetic
upstream IdP. It checks metadata through a separately constructed provider and verifies that
SDK-generated authorization reaches the consent boundary without creating caller authority.
Its disposable database cleans registrations up. This regression coverage is not evidence
that deployed testing or staging DCR succeeded.
