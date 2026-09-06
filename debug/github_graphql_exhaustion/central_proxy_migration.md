# Central proxy migration

Server implementation checkpoint: 2026-09-06 05:37 UTC. Client-state statements
below describe that checkpoint. The subsequent [wyrm2 cutover](wyrm2_central_cutover.md)
replaced local MITM with the central relay, but Desktop compatibility remains
unresolved. Neither checkpoint establishes quota resolution.

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

At this server checkpoint the wyrm2 local proxy and exact-route mitigation remained active. No
Desktop restart, trust-store migration, capture deletion or local-proxy cleanup
was performed at this checkpoint.

## Separate changes and remaining gates

Credential [#5688](https://github.com/agentydragon/ducktape/pull/5688) merged at
04:16:03 UTC as `e0ce3819a9582c6cbf3732b2dc197a19023f7f5d`. Flux reported
`github-api-proxy-secrets` Ready/Healthy on that revision at 04:18:11 UTC; both
host Secrets exist with the expected `credentials.json` key. Its Bazel,
Pre-commit and Gazelle checks passed; this is not a claim that all checks
completed. Real wyrm2 credential authentication passed the central canary below;
rugged's runtime decryption and authentication remain unverified.

The default-off relay [#5689](https://github.com/agentydragon/ducktape/pull/5689)
merged at 05:03:14 UTC as `78370f3a55243aed403c56f6a7e6992cb460f54f`.
Its Bazel, Gazelle, Pre-commit, CodeQL and FHS/NixOS client checks passed;
the artifact/import check was still running at merge. It passed 11
real Squid 7.6/OpenSSL integration
cases, including nested TLS, parent authentication, rejected bad certificates
and names, no direct fallback, and old-to-new app-private CA migration:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/c411cf74-a9c0-4a18-9c06-1a3978f3d90b).
The generated Nix wrapper builds. The production Nix Squid binary's central-route
check passed below; actual host activation remains required.

Central runtime [#5690](https://github.com/agentydragon/ducktape/pull/5690)
merged at 04:43:38 UTC as `6b4172216861c39da1463249093f633b96296b1e`.
Its final-head Bazel CI passed in
[this invocation](https://app.buildbuddy.io/invocation/9949a020-fc03-55a8-86a5-8fbb428ce360),
including the actual OCI entrypoint under UID 1000 (`--help`, not a full
mounted-secret server boot). Authentication must precede origin
dials, outer TLS must never use the interception CA, proxy credentials must not
reach raw flows, and public-origin validation must reject local/private targets
including DNS rebinding. The real deployed route was checked separately below,
not inferred from source tests. A denied CONNECT followed by a missing-auth
HTTP request on the same open connection is also tested: the successful-tunnel
credential cache must not be installed by a denied CONNECT.

The owning devel workflow successfully published
`git.allegedly.works/ducktape-ci/github-api-proxy:devel-20260906044947-6b41722`
at 04:50:09 UTC, digest
`sha256:fe054926fb3e02511ac68c513dddafd544ba9ac224226b5efffe4d0e8a0c6f7f`:
[image publication job](https://github.com/agentydragon/ducktape/actions/runs/34012234305/job/101430454299).
The image test gate, OCI build and registry push succeeded. A separate local-only
test follow-up booted the full image twice as UID/GID 1000 with read-only
synthetic TLS/CA/credential mounts, memory-backed working configuration and one
shared writable capture directory. Readiness and the authenticated verified-TLS
probe succeeded; capture appended across restart, remained mode 0600/owner 1000,
and contained no proxy password:
[mounted-secret smoke invocation](https://app.buildbuddy.io/invocation/8ba8242b-1543-46ab-a721-da3a903c13c3).
This does not verify actual Kubernetes storage permissions or production routing.

## Gateway decision

The deployed endpoint is `github-proxy.allegedly.works:8443`, on a dedicated
TLS-only Gateway. The shared Gateway already reports `ProtocolConflict` and
`Accepted=False` on both its wildcard HTTPS and exact TLS-passthrough listeners
on port 443, despite top-level `Programmed=True`. Do not add another overlapping
443 listener or treat the top-level condition as per-listener proof. The actual
deployment is Cilium 1.19.6; a similar overlap is described in the
[upstream report](https://github.com/cilium/cilium/issues/47590).

The separate Gateway leaves existing listeners untouched. Its Service, metrics
PodMonitor, private capture PVC and egress policy are wired under
`cluster/k8s/github-api-proxy/app/` in the deployment change. The Deployment
uses the published image above, one capture writer and Recreate rollouts. Storage
must retain evidence on app removal. Capture-write failure must remain observable
without a liveness restart erasing the sticky readiness failure.

Deployment [#5691](https://github.com/agentydragon/ducktape/pull/5691) connects
the app to root Flux, the registry credential reflection allowlist and Forgejo
image automation. It merged at 05:14:39 UTC as
`61e12ac76334e6bfdf21d485caa589cf77b9a264`. The app, identity and secret
Kustomizations reconciled successfully; the app was Ready/Healthy on
`b387eb535fc8cc05a41418c5135310361fdd205e` by 05:21:52 UTC. Its one Ready pod
started at 05:20:36 UTC with no restarts and the exact published image digest
above. The 100Gi RWX SeaweedFS capture PVC is Bound.

Its first CI run caught a missing explicit Goldilocks namespace decision.
Recommendations are now enabled with VPA update mode `off`: resource advice
must not automatically resize or evict the capture writer. The corrected cluster
integration, Flux-build, generator-namespace and proxy-alert tests all passed:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/f981fc8d-bf9c-4cce-a8f1-a70f44c729b4).
Runtime configuration lives in native `app/config.json`, mounted through a
generated ConfigMap; Kustomize updates the Deployment reference on config changes.

The actual alert expressions passed upstream promtool scenarios for healthy
operation, sticky capture failure, scrape failure, disappearance, replacement
and low storage. The final fixture uses the runtime's `session_ws` channel name:
[BuildBuddy invocation](https://app.buildbuddy.io/invocation/5231c189-9909-493f-b3bf-6f6385833c3c).
The initial runner failure was clone-local BuildBuddy metadata selecting an old
unpublished notes branch. Correcting only this clone's default base to a local
`devel` tracking `origin/devel` restored normal public-base plus patch execution;
no private branch was pushed.

## Live central route and remaining host boundary

The dedicated listener is Accepted/Programmed/ResolvedRefs with one attached
route; the TLSRoute is Accepted/ResolvedRefs. All five advertised public IPv4
addresses independently passed hostname-valid outer TLS and rejected an
unauthenticated proxy request with 407. No IPv6 address was advertised.

A separate loopback canary on wyrm2 used the actual Nix Squid 7.6 binary,
production config renderer and privately decrypted wyrm2 SOPS credential. It did
not replace the live Desktop proxy or change Desktop's trust/profile:

- Authenticated readiness through the relay returned 200.
- Verified nested TLS to the exact blocked Claude batch-status route returned
  429 and `Retry-After: 3600`. The synthetic body was empty; it requested no real
  GitHub batch work.
- One unauthenticated GitHub REST `/zen` request and Claude `/robots.txt` both
  returned 200. No GraphQL request was used for this probe.
- A deliberately false proxy password returned 407. The deliberately wrong
  outer CA failed TLS verification; no insecure bypass was used.
- Private metrics recorded the authenticated `wyrm2-desktop` identity, target
  route 429 and allowed GitHub request 200. Mimir retained these counters and
  ten successful scrape samples between 05:21:37 and 05:26:07 UTC.
- An in-pod comparison found neither configured proxy password, its Basic-auth
  representation, `Proxy-Authorization` nor legacy proxy-auth metadata in the
  raw capture. Only boolean results were returned, not credential/capture data.
  Raw and incremental session files are mode 0600, UID/GID 1000. Mount-root
  directories are not 0700; do not infer directory privacy from file modes.

Incremental session metadata exists, but no real Desktop WebSocket traffic has
yet been proved through the central route. No live restart/append test or alert
notification delivery test has been performed. The deployed SeaweedFS CSI lacks
volume-statistics support: the kubelet-based low-space alert has no input series.
A separate correction must use available native storage metrics with explicit
accounting limits; missing metrics do not mean empty storage.

Host opt-ins and the verified public CA pin are built and ready for review now
that the central-route gate has passed. Both hosts' generated units run only
the relay: central owns interception, capture and blocking. The actual normal
Desktop wrapper built for both hosts, preserving the normal profile and OAuth
handler. Rugged is not yet a verified reachable/activated client.

Both hosts use NixOS-inline Home Manager with `useUserPackages=true`. Directly
executing a newer HM activation changes the entire home generation but leaves
NixOS-owned Desktop packages old; the old system HM boot service can restore
the previous generation. Its generated `DRY_RUN=1` is not safely read-only:
some hooks still restart services or authenticate. A reviewed NixOS generation
switch needs an actual activation-scope audit, not just a package build. A
temporary scoped canary is not permanent declarative activation. Both existing
`80`/`90` overrides and all four block/WebSocket GC roots require exact ownership
checks before retirement; the old local mitigation remains running meanwhile.

The older Hubble-loss PR #5664 remains mergeable without conflicts. Its Bazel,
CodeQL, Gazelle and Pre-commit checks passed; its artifact/import check was
cancelled, not passed. No blind rerun was triggered.

## Finish order

1. Preserve the verified central route and close the storage-alert input gap.
2. Land the verified public CA pin and wyrm2/rugged opt-ins. Activate
   only a reachable host whose real relay/Desktop/OAuth route can be checked.
3. Remove only that verified host's obsolete local interceptor, owned overrides,
   old temporary package bridge, unused local signing keys and owned GC roots.
   Preserve captures, Desktop profile/sign-in and any operator replacements.
4. Continue account-wide quota and observation-coverage review for the agreed
   multi-day window. Migration by itself does not establish absence of quota
   exhaustions or capture completeness.
