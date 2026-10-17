# Central GoProxy feasibility — 2026-10-16

Scope: a transport experiment under `egress/x/goproxy`, independent of the runner sidecar Go
conversion. No replacement image, manifests, activation, merge or auto-merge. The current central
mitmproxy service remains authoritative. Task status lives in `plans/task_dag.md` (`GOPROXY_SPIKE`).

## Observed evidence

Base: `d558dcc30da97d6ddb2ad2f08767161ab545dc0a`. GoProxy v1.9.2 resolves to upstream commit
`a05e9220be0b6fd84566bf0d5f5880be94f3db36` (module checksum in `third_party/go.sum`).

- `bbr build //agentplane/egress/x/goproxy/cmd:goproxy_lab` succeeded on the initial lab patch,
  [invocation bef6a1bc-124e-4a5f-ac2d-08ccdcad63ba](https://app.buildbuddy.io/invocation/bef6a1bc-124e-4a5f-ac2d-08ccdcad63ba).
  This proves compilation, not socket behavior or complete service parity.
- Initial test invocation
  [100c8ec9-230d-407c-99b3-6855e2671357](https://app.buildbuddy.io/invocation/100c8ec9-230d-407c-99b3-6855e2671357)
  stopped during analysis: the CRD filegroup lacked visibility to the spike package. No test ran.
- Host DNS resolved `remote.buildbuddy.io` to `216.226.68.241` and the build completed after the
  parent lane's earlier connectivity failure. No certificate/proxy/RBE bypass was used.

## Source-grounded design, not runtime evidence

Pinned upstream [proxy.go](https://github.com/elazarl/goproxy/blob/v1.9.2/proxy.go),
[https.go](https://github.com/elazarl/goproxy/blob/v1.9.2/https.go) and
[http2.go](https://github.com/elazarl/goproxy/blob/v1.9.2/http2.go) provide CONNECT MITM,
inner request hooks, CONNECT `UserData` propagation, custom per-request RoundTripper, HTTP/2
streaming and response trailers. `AllowHTTP2` must be enabled. Default upstream transport skips
TLS verification and reads environment proxy settings: the candidate replaces it and blocks raw
CONNECT dialing. These APIs make the experiment plausible; they do not prove the contract.

The candidate uses `policy/api.PolicySet`, `PolicyBinding` and shared egress rule fields rather
than parallel Kubernetes shapes. It reuses `runner_gateway/go/auth.Resolver` and its existing
`TokenReviews` interface. The central proxy needs no Sandbox API lookup; proving a bound Pod is
TokenReview's job, so importing Sandbox resources into this path would add an irrelevant dependency.

The lab snapshot callback isolates the transport from a full informer rewrite. The executable
loads a private fixture snapshot per admission, binds loopback only and talks to a configured
TokenReview API. The Go socket tests control only the existing TokenReviews dependency. The envtest
case runs real kube-apiserver and etcd for installed CRDs, list/watch and binding deletion; its
authentication still uses the controlled reviewer. **This does not prove bound ServiceAccount token
issuance, real signature validation, or Kubernetes Pod deletion/replacement semantics.** Those need
separate real-cluster authentication coverage (the existing Kubernetes integration tier), or an
explicit envtest issuer/key/bound-object setup with equivalent evidence.

## Deliberately unported service behavior

Exact hosts, methods and paths plus whole-value header targets form the supported test subset.
Wildcards, path globs, all other credential presentations/sources, EgressCredential/Secret watches,
watch freshness bookkeeping, status writing, decision history, rules/admin APIs, health/readiness,
metrics, production address pin caching and bounded graceful drain remain unimplemented.
Snapshot expiry is tested as an enforcement input; it is not proof of production watch health.
The lab creates a transport per admitted request to prevent connection pooling/coalescing from
bypassing its request pin; throughput/pooling costs remain an explicit production design question.
IPv6 and complete reserved-address policy require further work before any production use.

## Decision pending socket evidence

Do not select GoProxy for central production replacement from compilation or source inspection.
The spike must report actual protocol/security outcomes and any required upstream patches or
wrappers, then stop with a bounded recommendation. A service port would be a separate decision.
