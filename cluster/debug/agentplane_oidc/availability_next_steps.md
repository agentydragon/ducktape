# Staging availability: scoped next steps

Investigation only, 2026-09-11 UTC. No routing, policy, replica, or shutdown
configuration changed. Companion observations are in
[staging recheck](staging_recheck_20260911.md); earlier packet evidence is in
[local Gateway TLS](local_gateway_tls.md).

## Findings

Two independent mechanisms remain after the CNP repairs:

- Authentik's public DNS includes the caller's own Gateway node. Verified-TLS
  probes from both app and Actions reset on that local public IP, while remote
  node and Gateway Service requests succeed. Adding backend permissions again
  cannot repair this demonstrated path.
- Both Deployments use `Recreate`. App Uvicorn has no explicit graceful shutdown
  timeout, while browser streams can stay open indefinitely. On this rollout,
  deployment availability became false at 02:45:40 UTC and true at 02:48:34:
  approximately 174 seconds with no available replica. This interval includes
  termination and startup, not solely stream draining. The new app pod was
  Ready when rechecked; its Kubernetes termination grace is 30 seconds.

## Routing proposal: workload-scoped canary, not shared DNS

The smallest probe is an isolated app/Actions canary with a Pod `hostAliases`
entry mapping only `auth.allegedly.works` to the existing Gateway Service IP,
while retaining canonical issuer URLs, Host, SNI, and certificate verification.
This exercises every library's resolver, including Authlib and synchronous JWKS
fetches, without custom transports scattered across those clients.

This is not yet an approved permanent deployment pattern:

- The current Service IP is `10.106.122.5`; a literal alias couples clients to
  its lifetime. Before shipping, choose an explicit declarative owner for this
  stable address or a generation/reconciliation mechanism. Do not hard-code the
  observed address and forget Service replacement.
- Keep the canary on verified Gateway nodes. Current app/Actions select
  `topology.kubernetes.io/zone=hil-ovh`; independently verify this matches the
  Gateway's eligible nodes. Non-Gateway nodes lacked Service reachability in
  [the DNS scope audit](service_dns_scope.md).
- Do not rewrite shared CoreDNS: other Authentik clients on non-Gateway nodes
  would inherit an unverified, previously failing route.
- A Cilium datapath repair is the durable alternative if workload-scoped routing
  cannot meet placement/address ownership requirements. The earlier packet
  evidence is suitable for a reduced upstream reproduction, not proof that a
  global source-address-preservation toggle is safe.

Acceptance before changing live app/Actions: repeated verified discovery and
JWKS requests, actual operator login/token exchange, wrong-SNI and direct
plaintext rejection, both client workloads, eligible-node relocation, and
Service replacement behavior. Do not create canary credentials manually or use
the operator's stored bearer tokens for synthetic requests. Rollback removes
only the scoped override; that restores the known intermittent public path.

## App: bound shutdown before attempting overlap

`x/agentplane/app/main.py` constructs Uvicorn without
`timeout_graceful_shutdown`; bridge teardown follows `serve()`. First implement
an explicit bounded HTTP/SSE shutdown budget with remaining time for bridge and
database cleanup inside Kubernetes' grace period. Test an open conversation and
inventory stream through SIGTERM, reconnection with Last-Event-ID, and continued
runner execution. This bounds outage duration; it does not eliminate the
`Recreate` startup gap.

Do not simply switch the app to `RollingUpdate`:

- `RunnerBridge.start()` attaches to every running session before the HTTP
  server starts. Each replica has its own in-memory feeds and locks.
- `runner/session.py:Session.attach()` supersedes the previous attachment;
  `runner/service.py` sends the old attachment an error. The new replica can
  displace the old one's feeds before becoming Ready, and commands routed to
  the old replica can attach again.
- `TrajectoryStore.thread()` does select-then-insert under a unique constraint,
  not an upsert; overlapping first attachment can race. Event insertion is
  replay-idempotent, which does not solve attachment ownership.
- Thread-change notifications are process-local. Persisted sessions alone are
  insufficient evidence of replica-safe live updates.

Next probe: a two-app/one-runner fixture with a live session, alternating command
requests, new-thread creation, and rolling termination. Choose a protocol with
independent read subscriptions and serialized commands, or an explicit fenced
bridge owner with command routing. Only then evaluate `maxUnavailable: 0`,
`maxSurge: 1`; include the temporary extra resource reservation.

## Actions: a separate rolling-update candidate

Actions already fences admission at signal receipt, budgets execution drain,
and configures a five-second Uvicorn shutdown within a 60-second pod grace.
Its store uses execution leases; tests cover another replica claiming queued
work and cross-replica push delivery (`test_executor_liveness.py`,
`test_push.py`, `test_waits.py`). OAuth storage is PostgreSQL-backed.

A focused candidate is `maxUnavailable: 0`, `maxSurge: 1`, with a two-replica
rollout test proving no duplicate execution or push delivery and successful
OAuth refresh across old/new replicas. Alembic's advisory migration lock
serializes migration runners, but does not guarantee old-binary compatibility
with the new schema. Require expand/contract migrations and compatible
catalog/configuration before overlapping versions. Keep the current strategy
until that acceptance passes; do not infer app safety from Actions safety.

## Order of work

1. Land safe federation/SSE diagnostics independently so failures retain their
   actual cause and status.
2. Review the scoped routing canary and its address/placement ownership.
3. Implement and test bounded app shutdown as a separate change.
4. Validate Actions overlap; treat app attachment ownership as a separate
   protocol decision, not a deployment-YAML tweak.
