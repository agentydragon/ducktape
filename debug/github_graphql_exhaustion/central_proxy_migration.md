# Central proxy migration

Implementation checkpoint: 2026-09-06 04:43 UTC. This is not a quota-resolution
or client-migration claim.

## Landed and verified

- Identity foundation [#5684](https://github.com/agentydragon/ducktape/pull/5684)
  merged as `e4ad860c91b254dd07444daf2a0e8e8d0793fe1d`.
- Flux `github-api-proxy-identity` was Ready/Healthy at 04:09:09 UTC on
  `5de89047e62f72e57b3617203e2036ec6d3ca22d`, which includes that merge.
- Both central Certificates are Ready. The public endpoint certificate has the
  exact `github-proxy.allegedly.works` SAN and a Let's Encrypt issuer.
- The dedicated interception CA's public certificate was retrieved through the
  authenticated Kubernetes API, without requesting or copying its private key.
  Its SHA-256 fingerprint is
  `6F:51:AD:39:F5:B9:7D:B7:E2:FB:6D:96:50:91:70:7F:74:CC:05:CB:1F:DB:90:6E:36:D2:A1:8E:3B:54:0C:55`.

The existing wyrm2 local proxy and exact-route mitigation remain active. No
Desktop restart, trust-store migration, capture deletion or local-proxy cleanup
was performed at this checkpoint.

## Separate changes and remaining gates

Credential [#5688](https://github.com/agentydragon/ducktape/pull/5688) merged at
04:16:03 UTC as `e0ce3819a9582c6cbf3732b2dc197a19023f7f5d`. Flux reported
`github-api-proxy-secrets` Ready/Healthy on that revision at 04:18:11 UTC; both
host Secrets exist with the expected `credentials.json` key. Its Bazel,
Pre-commit and Gazelle checks passed; this is not a claim that all checks
completed. Real proxy authentication remains a deployment gate.

The default-off relay is published in
[#5689](https://github.com/agentydragon/ducktape/pull/5689), head
`a40bccd04ff26406663d1e431d9a006d5b793e60`; PR CI remains pending. A CodeQL
finding in the synthetic TLS fixture was repaired by setting its explicit
TLS 1.2 minimum; the new head's Bazel CI passed and CodeQL no longer reported
that failure. It passed 11
real Squid 7.6/OpenSSL integration
cases, including nested TLS, parent authentication, rejected bad certificates
and names, no direct fallback, and old-to-new app-private CA migration:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/c411cf74-a9c0-4a18-9c06-1a3978f3d90b).
The generated Nix wrapper builds; actual host activation and the production Nix
Squid binary's central-route check are still required.

Central runtime is published in
[#5690](https://github.com/agentydragon/ducktape/pull/5690), head
`77379cc3b9301d0c16bb7723b2d20aa59e20fcdb`. Its final-head Bazel CI passed in
[this invocation](https://app.buildbuddy.io/invocation/9949a020-fc03-55a8-86a5-8fbb428ce360),
including the actual OCI entrypoint under UID 1000 (`--help`, not a full
mounted-secret server boot). Authentication must precede origin
dials, outer TLS must never use the interception CA, proxy credentials must not
reach raw flows, and public-origin validation must reject local/private targets
including DNS rebinding. Image publication and the real deployed route remain
gates, not assumptions inferred from source tests. All CodeQL, Gazelle and
Pre-commit checks passed; the artifact/import check remained running in its
generated-command-policy Nix step. A denied CONNECT followed by a missing-auth
HTTP request on the same open connection is also tested: the successful-tunnel
credential cache must not be installed by a denied CONNECT.

## Gateway decision

The planned endpoint is now `github-proxy.allegedly.works:8443`, on a dedicated
TLS-only Gateway. The shared Gateway already reports `ProtocolConflict` and
`Accepted=False` on both its wildcard HTTPS and exact TLS-passthrough listeners
on port 443, despite top-level `Programmed=True`. Do not add another overlapping
443 listener or treat the top-level condition as per-listener proof. The actual
deployment is Cilium 1.19.6; a similar overlap is described in the
[upstream report](https://github.com/cilium/cilium/issues/47590).

The separate Gateway leaves existing listeners untouched. Its Service, metrics
PodMonitor, private capture PVC and egress policy are prepared under
`cluster/k8s/github-api-proxy/app/`, but are not wired into Flux. The Deployment
must use a published image, one capture writer and Recreate rollouts. Storage
must retain evidence on app removal. Capture-write failure must remain observable
without a liveness restart erasing the sticky readiness failure.

The complete Deployment template is saved locally outside the repository at
`/tmp/github-api-proxy-deployment-5213.yaml.in`. It deliberately has no usable
image reference yet. Replace its publication marker with the verified registry
tag before adding it to the app and wiring root Flux. Do not publish a guessed
image tag or claim the directory deploys a running proxy.

The actual alert expressions passed upstream promtool scenarios for healthy
operation, sticky capture failure, scrape failure, disappearance, replacement
and low storage. The final fixture uses the runtime's `session_ws` channel name:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/5231c189-9909-493f-b3bf-6f6385833c3c).
The initial runner failure was clone-local BuildBuddy metadata selecting an old
unpublished notes branch. Correcting only this clone's default base to a local
`devel` tracking `origin/devel` restored normal public-base plus patch execution;
no private branch was pushed.

Host opt-ins and public CA pin are built, committed locally in
`/tmp/ducktape-github-proxy-host-optin-5213`, and deliberately unpublished until
central-route verification. The activation report is
`/tmp/github-proxy-host-optin-validation-20260906.md`. Full NixOS-inline Home
Manager switching may include unrelated changes; a scoped canary is not proof
of permanent declarative activation. Both existing `80`/`90` overrides and all
four block/WebSocket GC roots require exact ownership checks before retirement.

The older Hubble-loss PR #5664 remains mergeable without conflicts. Its Bazel,
CodeQL, Gazelle and Pre-commit checks passed; its artifact/import check was
cancelled, not passed. No blind rerun was triggered.

## Finish order

1. Land the independently tested runtime/image and default-off relay changes;
   verify image publication. Credentials have already reconciled.
2. Complete and reconcile the central Deployment, networking and metrics. Verify
   the dedicated listener and route conditions, then authenticated nested TLS,
   exact-route blocking, raw credential redaction and incremental capture.
3. Pin the verified public CA and opt in wyrm2/rugged declaratively. Activate
   only a reachable host whose real relay/Desktop/OAuth route can be checked.
4. Remove only that verified host's obsolete local interceptor, owned overrides,
   old temporary package bridge, unused local signing keys and owned GC roots.
   Preserve captures, Desktop profile/sign-in and any operator replacements.
5. Continue account-wide quota and observation-coverage review for the agreed
   multi-day window. Migration by itself does not establish absence of quota
   exhaustions or capture completeness.
