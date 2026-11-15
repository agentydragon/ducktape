# EnvironmentService process fixture

`process.py` runs the production every-replica listener from `internal/serving` against a supplied
Kubernetes API server. App integration and browser tests use an independent EnvironmentService peer
with the App SVID and the Controller server identity. Session and Runtime-proxy peers remain at the
Gateway. Each process owns its listener, policy watches and bounded shutdown.

The test binary omits the leader-only reconcilers: these tests set Sandbox status and run scripted
Runtime processes themselves. It implements no replacement RPC handlers or authorization. The k3s
suites exercise the actual Controller manager and deployment composition. No fixture changes a
production manifest or deployed identity.
