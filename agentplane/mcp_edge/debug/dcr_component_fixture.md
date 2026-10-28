# Smaller DCR fixture

## Result and scope

The real Python MCP SDK completes discovery and registration against the production
Go OAuth and MCP handlers with PostgreSQL, without k3s. The probe reuses the existing
auth-server integration fixture and the unchanged SDK wire client, relocated from
`system_tests/dcr.py` to `mcp_edge/authserver/dcr.py`.
Both loopback and HTTPS redirect cases persist a registration in the real database.

The upstream OIDC provider is the auth-server tests' fake, used for startup discovery.
Registration stops before browser authorization, as the existing system scenario does.
No Action Service, Sandbox, runner, Kubernetes API, or image deployment is exercised.

The SDK component target owns the protocol assertions; the independent k3s smoke owns
the built edge, configuration, Service/port-forward, discovery, and registration.
See [coverage and limits](../../system_tests/dcr.md). A full sweep runs both targets;
the expected benefit is narrower selection for ordinary SDK-helper changes.

## Initial probe evidence

Base: `540bc64f8f82576662c217b94eecd18f69f25b0d`, with the local probe patch mirrored by
`bbr`. BuildBuddy's commit field identifies that base, not a published candidate head.

[Fresh paired RBE run](https://app.buildbuddy.io/invocation/3851e6ff-fb83-48e2-83ec-6dff3d1b08ca):

| Target                                              | Test action | Result            |
| --------------------------------------------------- | ----------: | ----------------- |
| `//agentplane/mcp_edge/authserver:sdk_dcr_test`     |       25.0s | Passed            |
| `//agentplane/system_tests:test_dcr`                |       72.3s | Both cases passed |
| `//agentplane/mcp_edge/authserver:integration_test` |       11.5s | Passed            |

All three used `--nocache_test_results` in the same invocation. The k3s log reports
48.5s to stack readiness and 55.24s inside pytest. The 47.3s action difference is one
controlled observation, not a fleet estimate or end-to-end CI saving. Build analysis,
runner admission, cache state, and other critical-path tests remain separate costs.

A [fresh rerun of the finalized entrypoint](https://app.buildbuddy.io/invocation/38e55ad8-0cef-4972-9bb0-0612ec782d9e)
also passed both registrations in a 29.0s test action. The two component observations
are 25.0s and 29.0s; there is only one fresh k3s baseline in this experiment.

[Explicit Python library checks](https://app.buildbuddy.io/invocation/d8168306-8be0-42ff-ab2f-58944da6fcb1)
passed for `system_tests:{dcr,dcr_probe,test_dcr_lib}`. Pinned pre-commit checks passed.
The [final entrypoint library check](https://app.buildbuddy.io/invocation/be74ba3d-c131-4fee-acd6-a02e04848fe4)
records the subsequent explicit `dcr_probe` build.

Reproduce:

```bash
bbr test //agentplane/mcp_edge/authserver:sdk_dcr_test \
  //agentplane/mcp_edge/authserver:integration_test \
  //agentplane/system_tests:test_dcr --nocache_test_results --test_output=errors
```

## Dependency boundary

The SDK protocol assertions stay in the component test: challenge, metadata chain,
same-origin registration, JSON/201, persisted public-client fields, PKCE/state, and
absence of a token. The independent k3s HTTP smoke checks the built edge, generated
configuration, Service/port-forward, challenge, discovery, and a real registration.
It does not import or copy the SDK helper.

Before the split, only `test_dcr.py` consumed `system_tests:dcr`; `test_mcp` has a
separate operator OAuth flow. The relocated helper now belongs only to the component
target. Ordinary SDK-helper edits can select it without selecting k3s. Edge/image/config
edits and broad CI triggers can still select the system tests. Keeping the current
helper in both targets would add cost without establishing this dependency boundary.

The initial timing probe alone does not establish affected-target selection or the
combined component/smoke cost; those require validation of the promoted split.
