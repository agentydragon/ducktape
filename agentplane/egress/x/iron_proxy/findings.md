# iron-proxy reuse comparison

Compare Agentplane `devel` at `4fdc22d821c57f85a6e4bc11a0bf8b8d4f0e270b` with iron-proxy
[`7c6a6e9261f09aa4f4efdb0bc20bdbbfb306603d`](https://github.com/paradigmxyz/iron-proxy/tree/7c6a6e9261f09aa4f4efdb0bc20bdbbfb306603d).
The executable experiment is [`README.md`](README.md).
It changes no deployed service or production proxy code. Upstream `60ed3566` adds only a Go
toolchain/build update over the pinned Iron revision; its transport and transform sources are unchanged.

## Recommendation

Reuse is feasible. Agentplane's Kubernetes policy does not require owning the whole transport:
the existing iron-proxy gRPC transform can delegate to Agentplane's actual `policy.Index` and
`auth.Resolver`. Its internal packages can also be imported directly through Bazel after exposing
their generated targets; this experiment compiles them from an ordinary Agentplane package.
There is no need to copy the proxy source or impersonate an iron-proxy Go package to do that.

The pinned proxy is not a drop-in replacement. A small response-flush patch passes the interactive
TLS HTTP/2 case that stock iron-proxy fails. That failure alone is therefore insufficient reason
to own a separate proxy. More substantial seams remain around address admission, body handling,
cleartext HTTP/2, trusted identity state, and draining.

For a replacement candidate, prefer an in-process Agentplane transform plus narrow upstream
transport/context hooks. The external gRPC adapter below is a working integration route, but adds
an RPC, a secret-bearing interface, and server-side tunnel state. Keep the current central proxy as the
comparison baseline until those remaining transport requirements have executable acceptance.
This experiment does not establish that a fork is cheaper to maintain, or that either proxy has
better production throughput, latency, memory use, or security history.

## Code size

A mechanical comparison of the current central proxy with the existing external gRPC bridge saves
**228 production Go code lines (about 7%)**, before implementing the remaining replacement work.
Measured with `tokei 14.0.0`, excluding blanks and comment-only lines, at Agentplane
`cff612c4e6827b8af45e76856ff50d671598396c` and upstream Iron
`60ed3566e5b39b1ef74d4854c62eb2d9fc4b8f42`. The latter has identical Go runtime sources to the
spike's pinned Iron revision.

The central-backend scope is non-test Go under `agentplane/egress/`, excluding `sidecar/` and `x/`.
The replacement column adds only `x/iron_proxy/bridge/bridge.go` and the maintained streaming patch.
This compares production paths; the bridge is already tracked as a spike, so this is not a predicted
git diff. Shared packages outside egress, Python, protobuf definitions, generated output, build files,
and deployment configuration are outside the count.

| Component                                                 | Current proxy | Iron plus external gRPC bridge |
| --------------------------------------------------------- | ------------: | -----------------------------: |
| Native transport, admission gate and lifecycle (`proxy/`) |           629 |                              0 |
| Agentplane gRPC bridge                                    |             0 |                            391 |
| Maintained streaming patch (net code added)               |             0 |                             10 |
| Other central backend Go                                  |         2,628 |                          2,628 |
| **Total**                                                 |     **3,257** |                      **3,029** |

The streaming patch adds 12 code lines and removes two. Counting comments and blanks instead, the
native proxy is 776 physical lines, the bridge is 453, and the patch adds 13 net physical lines:
310 fewer physical lines in this same comparison.

This is a prototype comparison, not a production replacement forecast. Policy evaluation, credential
substitution, Kubernetes authentication and watchers, policy-aware upstream resolution, history and
APIs largely remain ours. RPC authentication and wiring, policy-aware DNS/dial hooks, projected-token
support, tunnel-state cleanup, draining, h2c and history integration still need work. Only 228 code
lines of savings remain before those additions erase the reduction; some startup/configuration code
might also shrink, but that has not been implemented or credited here.

Current `proxy/` tests contain 1,494 Go code lines; all Iron spike tests contain 1,204. The spike covers
less behavior, so that difference is not a test-code saving: the existing security and lifecycle
coverage must be preserved or ported. Python end-to-end tests are outside these counts.

Iron itself contributes 12,614 handwritten production Go code lines under `internal/` and `cmd/`,
plus 806 generated lines under `gen/`, excluding tests. Reuse would trade our transport implementation
for that dependency and a small maintained patch. The stronger case is upstream functionality and
shared maintenance, rather than a large reduction in Agentplane-owned code.

## Executed comparisons

[BuildBuddy invocation `27357bf8`](https://app.buildbuddy.io/invocation/27357bf8-7cc7-4acf-8670-8b2ce546041d)
executed and passed all four test targets against Agentplane `dacc69719d`: the original and
patched transport comparisons, the gRPC bridge including its Iron CLI subprocess, and the current
`//agentplane/egress/proxy:proxy_test`. None of the four test results came from cache.
[BuildBuddy invocation `9a21aa90`](https://app.buildbuddy.io/invocation/9a21aa90-9c44-4b8a-9843-ccd654471ac6)
executed and passed the extended bridge suite, including all combined CLI streaming cases below.
The original and patched transport comparison targets also passed from cache in that invocation.

| Scenario                                                                 | Pinned iron-proxy                        | With response-flush patch                              |
| ------------------------------------------------------------------------ | ---------------------------------------- | ------------------------------------------------------ |
| Direct Bazel import of proxy, transform, certificate cache and DNS guard | Compiles and runs                        | Compiles and runs                                      |
| Ordinary HTTP forwarding                                                 | Pass                                     | Pass                                                   |
| Finite TLS HTTP/2 response and gRPC trailers                             | Pass                                     | Pass                                                   |
| Upstream flushes initial metadata and waits for a client message         | Client waits until bounded cancellation  | Metadata and an echoed frame arrive before request EOF |
| Prior-knowledge h2c inside CONNECT                                       | Protocol refusal before upstream request | Same refusal                                           |
| Reading a capped `BufferedBody`, then forwarding it                      | Silent truncation                        | Same truncation                                        |
| Leaving that body unread by transforms                                   | Full original body streams               | Full original body streams                             |

The stock streaming test first establishes that the upstream flushed headers; a timeout elsewhere
does not satisfy the probe. The patched test exchanges a response frame while the request body is
still open. Neither is a throughput benchmark. The tests deliberately assert the observed stock
limitations, so a green characterization suite is not a claim of replacement parity.

`bridge:bridge_test` starts both the stock and patched Iron CLIs as separate processes. Its real
gRPC `FullDuplexCall` client and server communicate through CONNECT and TLS interception, with a
separate upstream trust root:

- **Patched transport, header-only transform:** initial metadata reaches the client before its
  first request message; three request/response exchanges finish before request EOF. Credential
  substitution and private-header stripping, clean EOF and trailers, client cancellation and
  deadline propagation to the upstream all work. Removing the binding refuses the next stream on
  the same tunnel. That refusal currently surfaces as gRPC `INTERNAL`, not `PERMISSION_DENIED`;
  the test characterizes this framing gap and checks that no upstream call occurs.
- **Stock transport, header-only transform:** the authenticated upstream flushes initial metadata,
  but the client receives none before its deadline. This reproduces the missing-flush blocker with
  the actual bridge in the path.
- **Patched transport, `send_request_body: true`:** the client reaches its deadline before the inner
  request reaches either policy evaluation or the upstream. CONNECT alone was reviewed. Forwarding
  the body into the unary transform therefore still blocks an interactive stream before request EOF.

These are characterization tests: the two expected stalls and the refusal-framing gap do not count
as replacement parity. They do not change the deployed proxy or add conditional body handling.

The existing JSON/HTTP fixture also checks:

- CONNECT authentication reaches the real Agentplane resolver through iron-proxy's own gRPC client.
- An inner HTTP/1.1 request inherits that identity without presenting the bearer again.
- The real policy evaluator selects the credential and rewrites the exact header and JSON field;
  the fixture upstream receives the real value and receives neither private hop header.
- Revoking the controlled TokenReview result refuses the next request on that same tunnel, before
  a second request reaches the upstream.

The bridge uses the shared PolicySet reader and its freshness gate. The JSON fixture uses plaintext
HTTP/1.1 inside CONNECT; the streaming cases verify TLS on both legs. Loopback is permitted only for
the fixture upstreams. These fixtures do not prove real Kubernetes token issuance/revocation,
production network isolation, or full protocol parity.

## Reuse paths and remaining costs

### Import packages into an Agentplane binary

[`third_party/iron_proxy`](../../../../third_party/iron_proxy/README.md) pins the source archive and
isolates its dependency versions. Gazelle visibility directives expose the selected `internal/`
packages. The tested build mode is `pure = "on"`, matching upstream's `CGO_ENABLED=0` releases;
otherwise the PostgreSQL parser's C binding needs additional Bazel wiring.

Importing the transport includes its configuration/control-plane, MCP, retry, and secret-provider
dependencies even when those features are unused. This is a real compile dependency, not a claim
that the features are enabled at runtime. Package extraction or upstream API changes could narrow
that dependency surface. These internal packages are not a supported public Go API.

The direct-import probe does not combine Agentplane's policy/auth libraries and Iron's isolated
dependency graph in one binary. That embedding still needs a build/type-compatibility check and
may require aligning shared dependency versions. The tested gRPC route avoids that type boundary.

The public proxy constructor binds its own listeners and does not expose the bound addresses or
accept prebound listeners; the probes obtain addresses from startup logs. Upstream trust uses the
system root loader rather than an injected root pool. A reusable server/listener and transport API
would make embedding cleaner.

### Use the external gRPC transform

The bridge reuses the existing evaluator and authentication resolver. It does not translate
PolicySets into a second policy language. The schema is taken from the pinned archive and generated
in Agentplane's Go graph; the Iron process uses its own generated types across the wire.

CONNECT annotations carry a random lookup handle. The bearer stays in bridge memory and is resolved
again for each inner request. The controlled TokenReview fixtures disable the resolver's acceptance
cache; the production resolver reuses a successful verdict for up to 30 seconds. The handle is read
only from this transform's own trace. Identity is not inferred from a source address, client header,
or the initial CONNECT host.

Costs that the prototype leaves open:

- **Trusted interface:** allowed transform responses contain real credentials. The endpoint must be
  authenticated and reachable only by trusted proxy instances; exposing it to a sandbox creates a
  credential-redemption interface. The loopback test is not that production authorization boundary.
- **State lifetime:** handles need bounded retention, expiry/close cleanup, and a replica-affinity
  or shared-state decision. There is no tunnel-close callback in the tested transform contract.
- **Projected audiences:** the prototype strips presented tokens but does not review/substitute
  additional destination-audience tokens.
- **Header presence:** Iron replaces headers only for a nonempty returned map. Empty-valued entries
  are needed to clear the final private header without accidentally requesting no modification.
- **Body handling:** `send_request_body: true` buffers all requests matching that transform. Its
  cap silently truncates; it cannot be treated as a rejecting size limit. Bodyless and streaming
  paths need a different configuration or a more expressive hook. An empty replacement body also
  needs explicit presence semantics in the wire API.

### Extend the transport

[`streaming.patch`](../../../../third_party/iron_proxy/streaming.patch) flushes response headers and
each body write. The same test sources build against the original and patched dependencies in
separate binaries. The combined CLI cases also establish the header-only bridge's interactive TLS
gRPC path. Buffering/backpressure and high-throughput behavior have not been benchmarked.

The patch does not provide h2c. It also does not integrate Agentplane's address admission:

- Iron's guard checks a global CIDR deny list at dial time. Its defaults allow private RFC1918
  addresses and do not express Agentplane's selected-rule `clusterInternal` distinction.
- Agentplane rejects a host if any DNS answer is forbidden, rechecks policy after DNS I/O, and dials
  the admitted address. A transform that resolves/checks a hostname before Iron independently dials
  it is insufficient to preserve that contract.
- Iron's `Options` does not accept a request-aware dial/round-tripper hook. A bounded extension
  would need to carry an admission result to the actual dial, including reuse/coalescing behavior
  and the separate WebSocket path.
- Protected per-connection context and explicit drain/close hooks could remove the adapter's
  annotation/state workaround. Their implementation cost has not been measured here.

## Shared policy concern

Agentplane's policy request has no scheme or port restriction. Its existing proxy tests permit
real-credential substitution into plaintext HTTP. Transport reuse does not fix that policy gap;
external credential-bearing routes need an explicit HTTPS/port contract with scoped internal
exceptions before relying on credential confidentiality on the upstream leg.
