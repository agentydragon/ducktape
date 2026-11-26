# EnvironmentService process fixture

`process.py` runs the production every-replica listener from `internal/serving` against a supplied
Kubernetes API server. App integration, browser, and Runtime tests use independent Environments peers
with the App SVID and Controller server identity. The fixture can enable the production Runtime proxy
on those peers; it connects to each Sandbox Runtime with the `spiffe://agentplane/runtime-proxy` SVID.
The Sessions fixture serves SessionDispatcher only, at a separate address. Each process owns its
listener, policy watches and bounded shutdown.

The test binary omits the leader-only reconcilers: these tests set Sandbox status and run scripted
Runtime processes themselves. It implements no replacement RPC handlers or authorization. The k3s
suites exercise the actual Controller manager and deployment composition. No fixture changes a
production manifest or deployed identity.

`servicetest.Launcher` composes the production handlers and method authorization for Actions'
orchestration tests. It returns only the EnvironmentService wire interface and server options;
consumers own their test transport and token reviewer without importing the serving implementation.
It constructs explicit resume without an automatic-wake budget or execution capability.
