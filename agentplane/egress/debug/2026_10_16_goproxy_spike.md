# Central GoProxy feasibility — 2026-10-16

Scope: a transport experiment under `egress/x/goproxy`, independent of the runner sidecar Go
conversion. No replacement image, manifests, activation, merge or auto-merge. The current central
mitmproxy service remains authoritative.

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

- Shared comparison [a23fe625-cd60-4079-a76a-859f1bb8ea6c](https://app.buildbuddy.io/invocation/a23fe625-cd60-4079-a76a-859f1bb8ea6c)
  ran six assertions: the three mitmproxy variants passed; the three Go variants were refused
  before transport testing because client-go sent protobuf TokenReview to the JSON-only fixture.
  The lab client now explicitly requests JSON. This was a fixture wire-format mismatch, not
  evidence about GoProxy streaming support.
- Go contract retry [9e6a8c82-2e30-4d2a-841d-bba25e1e416d](https://app.buildbuddy.io/invocation/9e6a8c82-2e30-4d2a-841d-bba25e1e416d)
  could not start: outside-sandbox client DNS failed for `remote.buildbuddy.io` after the wrapper's
  five retries. Further local retries stopped; draft PR CI owns the next exact-head validation.

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
metrics, complete refusal-reason/status projection, production address pin caching and bounded
graceful drain remain unimplemented.
Snapshot expiry is tested as an enforcement input; it is not proof of production watch health.
The lab creates a transport per admitted request to prevent connection pooling/coalescing from
bypassing its request pin; throughput/pooling costs remain an explicit production design question.
IPv6 and complete reserved-address policy require further work before any production use.

## First CI results and required patch

Draft [#10816](https://github.com/agentydragon/ducktape/pull/10816), head `690358ca34`, tested
with GitHub merge `e92ce8b97a33d5bb021009d392e7b7c61a96865c` in [CI 38995208063](https://github.com/agentydragon/ducktape/actions/runs/38995208063),
[test invocation 9be02e8c-1189-5f91-bcbf-f103aa8573e7](https://app.buildbuddy.io/invocation/9be02e8c-1189-5f91-bcbf-f103aa8573e7):

- `//agentplane/egress:test_proxy` passed all 47 cases, including identical current/Go HTTP
  whole-value substitution and interactive TLS/h2c gRPC reads before request EOF with trailers.
- `transport_test` ran and failed at the two TLS/h2c header-only-open response cases and the
  Go WebSocket client's duplex-body assertion. No other Go case reported a failure, including
  real envtest list/watch binding deletion on a held H2 connection, zero-TCP denial, bad CA/host,
  literal pin dialing and existing-connection binding/staleness/identity cleanup.
- The H2 failure is a real library behavior: `http2.go` writes initial response headers but
  waits on the first body read before flushing them. An upstream sending headers and waiting
  for a later client message can stall a client waiting for initial metadata. A tiny pinned
  `flush_h2_headers.patch` flushes after `WriteHeader`; the failing contract is retained.
- The WebSocket assertion was a test-client issue: Go `http.Client.Timeout` wraps the 101 body
  in a reader that hides its `io.Writer`. The fixture now uses a request context deadline,
  preserving the duplex body. The independent aiohttp WebSocket comparison remains required.
- Repository-wide test summary was 1466 passing, one failing target. This was a red run;
  compilation and unrelated passing tests do not make the spike accepted.

## Patched socket evidence and recommendation

Head `a90ec93bb5234d4a172e51c57bdee344610fa518`, GitHub merge
`599c46e769ed58fe2c671d2f8ac44ea7c48529ed` on base `d558dcc30d`, passed
[CI 38995643200](https://github.com/agentydragon/ducktape/actions/runs/38995643200),
[test invocation b9227412-40dc-5c15-bdc4-ea87e091838f](https://app.buildbuddy.io/invocation/b9227412-40dc-5c15-bdc4-ea87e091838f).
`transport_test` freshly passed in 8.3 seconds; full `test_proxy` freshly passed all 51 cases in 77.2 seconds (pytest body: 63.32 seconds).
The graph-wide run reports 1467 passing targets, of which these two executed and the others were
cached. Pre-commit and Nix wheel/import checks also passed. This is automated lab evidence;
no deployed central proxy was replaced or exercised.

| Contract                                              | Current mitmproxy and candidate                                         | Additional candidate evidence                                                                                                    |
| ----------------------------------------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Authenticated CONNECT and header substitution         | Identical real aiohttp clients and upstream assertions                  | Existing shared TokenReview resolver/interface                                                                                   |
| Intercepted TLS H2 gRPC and prior-knowledge h2c       | Write/read before request EOF, two messages, trailers                   | Raw H2 frames and required initial-header flush regression                                                                       |
| gRPC deadlines and cancellation                       | Client status plus observed upstream handler completion                 | Request context cancellation reaches upstream                                                                                    |
| Denied request inside admitted tunnel                 | Raw TCP accept counter stays zero; positive counter control             | Counted listener before TLS, with zero DNS resolution on path denial                                                             |
| Incremental SSE and WebSockets                        | First SSE frame arrives while upstream waits; two WebSocket exchanges   | Raw WebSocket frame relay                                                                                                        |
| Upstream TLS trust and hostname                       | Existing service coverage retained; no new shared certificate assertion | Reject unknown CA, interception CA and wrong hostname; no HTTP reaches upstream                                                  |
| Approved literal address used by actual dial          | No new shared resolver assertion                                        | `.test` name reaches approved listener with exactly one lookup; forbidden second answer cannot rebind; environment proxy ignored |
| Policy change during DNS and mixed forbidden answers  | No new shared resolver assertion                                        | Zero TCP accepts for post-DNS revocation and mapped link-local answer                                                            |
| Existing-connection revocation/staleness/auth cleanup | Existing current-service tests retained                                 | Binding deletion, snapshot expiry, invalid replacement bearer and changed Pod UID all refuse later streams                       |
| Real Kubernetes API policy source                     | Existing Python fake remains for comparison                             | Real CRD create/list/watch/delete, shared egress fields preserved, next held-connection request refused                          |

**Recommendation: GoProxy is feasible as the central transport library for a separately reviewed
service port, with the pinned header-flush patch and explicit wrappers.** A secure transport with
independent upstream roots and no environment proxy, a dial hook that receives only an approved
literal address, per-inner-request policy/auth gates, and a dedicated pinned h2c transport are
required. GoProxy's defaults are unsuitable at this boundary. Stock v1.9.2 alone fails the
initial-metadata contract; the retained regression now passes with the four-line patch.

The experiment does not authorize production replacement. Complete the documented policy,
credential, watch/freshness, API/history, error projection and lifecycle behaviors in a separate
port before discussing cutover. Preserve a decision after DNS, prevent H2 pooling/coalescing
from escaping the approved address, and keep metadata/body credentials bound to the current
principal. The lab avoids pooling by creating a transport per admitted request; production pool
ownership and throughput remain open. Upstreaming/removing the small patch is maintenance work,
not a reason to weaken the header-only-open contract. No performance claim was measured here.

## Envtest recommendation

Use the existing envtest assets and shared Go schemes for the real CRD admission/list/watch
contracts. This catches field loss and API validation behavior that the permissive fake cannot.
Retain the narrow TokenReviews interface as a controlled dependency for deterministic token-error,
identity-replacement and existing-connection cleanup contracts. Keep real bound-token issuance,
audience and Pod deletion/replacement semantics in the Kubernetes integration tier until an
explicit envtest issuer/key/bound-object setup proves those behaviors. Do not treat replacing a
policy store with envtest as a reason to remove authentication coverage or to rewrite every fixture.

No real deployment or traffic cutover was exercised. The spike's transport subset is independent
of the integration app and needs no Sandbox lookup, new authority, token issuer or RBAC grant.
