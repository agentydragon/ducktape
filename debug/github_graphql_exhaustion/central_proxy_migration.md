# Central proxy migration

Implementation checkpoint: 2026-09-06 04:22 UTC. This is not a quota-resolution
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
`331b2408040d4ca7e398f88ca2a7a7b02f2cc52c`; PR CI remains pending. It passed 11
real Squid 7.6/OpenSSL integration
cases, including nested TLS, parent authentication, rejected bad certificates
and names, no direct fallback, and old-to-new app-private CA migration:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/c411cf74-a9c0-4a18-9c06-1a3978f3d90b).
The generated Nix wrapper builds; actual host activation and the production Nix
Squid binary's central-route check are still required.

Central runtime verification is separate: its six Bazel test targets passed in
[this invocation](https://app.buildbuddy.io/invocation/661cf0a2-0fc4-430c-8b23-2bdd29ab9a43),
including the actual OCI entrypoint under UID 1000 (`--help`, not a full
mounted-secret server boot). Authentication must precede origin
dials, outer TLS must never use the interception CA, proxy credentials must not
reach raw flows, and public-origin validation must reject local/private targets
including DNS rebinding. Image publication and the real deployed route remain
gates, not assumptions inferred from source tests.

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
