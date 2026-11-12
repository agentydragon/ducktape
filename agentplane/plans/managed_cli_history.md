# Managed CLI access to retained History

Managed CLI commands that read retained Session data lack a History endpoint. `sessions get` and
`sessions observe-input` connect to both Dispatcher and History, while `sessions read-turns` uses
History alone ([CLI commands](../cli/sessions.go), [`connectGRPC`](../cli/client.go)). The CLI test
[`TestObserveInputRequiresItsOwnHistoryConfiguration`](../cli/sessions_test.go) asserts that omitting
the URL exits with `AGENTPLANE_HISTORY_URL not set`. Current staging has no Sandbox to reproduce this
as a live failure, so the managed-workload gap below is traced from source.

`agent_cli_environment()` in [`platform_instructions.py`](../../cluster/cdk8s/agentplane/platform_instructions.py)
currently injects Actions, Sessions, Notifications and Egress URLs, but no History URL. Pod, VM and
Firecracker launch paths all use this helper ([`environment_sandbox.py`](../../cluster/cdk8s/agentplane/environment_sandbox.py),
[`environment_firecracker.py`](../../cluster/cdk8s/agentplane/environment_firecracker.py)). Inject
`AGENTPLANE_HISTORY_URL=http://<History Service FQDN>:8080`, following the existing Actions and
Dispatcher service URL pattern. The CLI speaks plaintext gRPC to the sidecar's exact-origin route;
the sidecar initiates SVID TLS to History. Add that origin to
[`workload_routes.sidecar_routes()`](../../cluster/cdk8s/agentplane/workload_routes.py), targeting that
Service with `HISTORY_SERVICE_URI`. The runner sidecar then presents its Workload SVID to History
inside the existing authenticated CONNECT; the central proxy carries opaque TLS. The egress chart
already includes History as a TLS-passthrough service, and History's NetworkPolicy admits the proxy
([`chart.py`](../../cluster/cdk8s/agentplane/chart.py),
[`history_service.py`](../../cluster/cdk8s/agentplane/history_service.py)).

The receiver already has the required Identity setup: the current History chart sets
`policy_namespace=env.namespace` and configures the SPIFFE listener
([`history_service.py`](../../cluster/cdk8s/agentplane/history_service.py)); `main.go` resolves
Identity-only reader SVIDs, and read RPCs admit them through `Methods()`
([`main.go`](../../agentplane/history_service/main.go),
[`Methods`](../../agentplane/history_service/api/service.go)). `scopedReader()` limits reads to
Sessions owned by the exact Identity name and UID. Keep that resolver and scope unchanged. The
managed Identity should use this SVID route with no History bearer credential or projected History
audience token. A Session ID or page cursor grants no authority. This path needs no `reader_accounts`
entry; preserve those configured accounts for existing callers and do not add an all-session reader
grant or wider reads across Identities.

Validate first in the existing cdk8s render test: assert all managed launch templates receive the
History URL, the sidecar route has the History Service upstream and URI, and no History credential
or projected audience token is added. Then add one app-unavailable system acceptance: a managed
Identity runs `agentplane sessions read-turns` against its own retained Session successfully, while
a second Identity and a same-name recreated Identity with a different UID are denied. Keep the system
overview traffic diagram current with the implementation.
