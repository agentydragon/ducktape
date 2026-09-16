# gRPC-Web spike

What a gRPC-Web transport would cost for `ThreadEvents`, measured rather than estimated, against
the Connect mount in <../thread_events.py> that serves the identical fold.

**It works.** `//x/agentplane/app/grpcweb_spike:test_grpcweb` passes: a browser-shaped gRPC-Web
request reaches a `grpc.aio` server through Envoy and streams frames back. Feasibility is not the
question; the costs below are.

## What the spike covers

- `server.py` — the same `ThreadEventsService.frames` fold, adapted to a `grpc.aio` servicer.
- `auth.py` — the caller check a gRPC interceptor has to run for itself.
- `envoy.yaml` — the translation hop, in the form a `CiliumEnvoyConfig` would carry.
- `test_grpcweb.py` — Envoy in a container, the gRPC server in-process, a gRPC-Web client on the
  wire a browser puts on the network.

## What it does not cover

Named so nobody reads the pass as more than it is:

- **No browser.** Chromium through Envoy is the question the spike was meant to settle and is the
  one piece not built; see "The testability cost" for what it would take.
- **No `CiliumEnvoyConfig`**, `Service` port, `NetworkPolicy`, or readiness probe for the second
  listener.
- **Envoy 1.35, not Cilium's build.** `envoy.filters.http.grpc_web` is present in
  `cilium/proxy`'s `extensions_build_config.bzl` on the v1.35 branch, so the filter exists; that
  this is the build Cilium 1.19.6 ships was not confirmed against the running DaemonSet.
- **The interceptor is not wired into the app**, and no test drives a real session cookie through
  it end to end. It also takes the wrong shape — see finding 3 — and is kept only as the evidence
  for that finding.

## Findings

**1. The fold is transport-neutral; its errors are not.** `frames` raises `ConnecpyException`,
which is Connect's vocabulary, so the gRPC adapter imports a Connect exception type to translate
it. Either the fold mints a third error type both adapters translate, or one transport's
exceptions leak into the other. Small, but it is the seam that makes "just swap the transport"
untrue.

**2. It is a second server.** In-process and sharing the store object, but its own listener on its
own port — so its own `Service` port, `NetworkPolicy` rule and readiness. The Connect mount is a
`app.mount()` on the ASGI app that already exists.

**3. Authorization is not a differentiator — retired.** `auth.py` reimplements what
`require_caller` already decides, and this finding first reported those 98 lines as a cost of
gRPC-Web. They are a cost of how the spike was written, and the finding does not survive at all.

The spike sends the browser's session cookie because that is what this app's browser credential
is: `oidc.py` and `operator_sessions.py` both say only a signed random handle crosses into the
browser, with the operator's access token kept in a Postgres row, and `fetch` ships that cookie
automatically. That is an XSS-exfiltration and revocation decision, deliberately made.

The conventional shape for authorized gRPC is a bearer token in call metadata, validated by
Envoy's `jwt_authn` filter -- which is in Cilium's build alongside `grpc_web`. It is reachable
without undoing the decision above: mint a short-lived RPC token from the cookie session in one
cookie-authenticated REST call and send that in metadata. The handle stays the root credential,
exposure is bounded to a short-lived token, the interceptor becomes JWT validation rather than a
second session reader, and RPC calls stop touching the session row -- which also removes the
refresh divergence this finding used to claim.

**None of that is transport-specific.** Connect over `fetch` sends the same cookie today, for the
same reason; bearer-versus-cookie is a header decision either transport carries identically. So
authorization does not distinguish the two, and nothing in this document should be read as saying
it does. `auth.py` is kept only as the evidence for this correction.

**4. Each gRPC service costs a `mypy.ini` exemption.** `mypy-protobuf`'s generated stub carries an
upstream unused ignore, so a consumer of it fails `warn_unused_ignores`. `mypy.ini` already
carried one entry for the runner's stub; the spike needed a second. The Connect generated module
needs none.

**5. The Envoy config is checked by nothing.** `envoy.yaml` must agree with the service name in
the proto, the port the server binds and the path the browser posts to. Nothing verifies that —
the first thing that reports a mismatch is a browser failing against the deployed cluster. It also
restates the stream timeout the gateway's `HTTPRoute` already carries.

**6. gRPC-Web does not give Python a real client either.** This was the main argument for moving.
`grpcio` is a mature client, but it speaks _native gRPC_ straight to the server, skipping the
translation hop — so a test using it covers the servicer and nothing about the wire the browser
reads. To cover the wire, `test_grpcweb.py` decodes gRPC-Web framing by hand, which is the same
5-byte envelope, for the same reason, as the Connect test it was meant to improve on. Both tests
in this file are here to show that contrast: one goes through Envoy, one does not.

**7. The test needs a container that dials back into the test process.** Nothing in this repo did
that before — the Postgres pattern is test-reads-from-container. Envoy runs with
`network_mode="host"` so `127.0.0.1` is shared. It works on RBE; it is a new shape of test
dependency.

## The testability cost

Today `//x/agentplane/app:test_thread_browser` runs real Chromium against the real app over the
**exact wire production uses**, because the RPC is in-process. With Envoy translating in
production, that stops being true. The test either grows an Envoy container — in an already-heavy
test that runs Postgres, the app process and a browser — or it exercises a path that does not
exist in production. The translation hop is precisely the part that cannot be reasoned about from
the source, and it is the part the test would stop covering.

## Measured

|                                      | Connect (in-process)      | gRPC-Web (Envoy)               |
| ------------------------------------ | ------------------------- | ------------------------------ |
| Service + adapter                    | 104 lines                 | 62 lines + a second server     |
| Authorization                        | no difference (finding 3) | no difference (finding 3)      |
| Proxy config                         | none                      | 71 lines, unverified           |
| `mypy.ini` entries                   | 0                         | 1 per service                  |
| New pinned images                    | 0                         | Envoy                          |
| Browser test covers production wire  | yes                       | no, without an Envoy container |
| Python client for the browser's wire | none (hand-decoded)       | none (hand-decoded)            |

## Reproducing

```bash
bbr test //x/agentplane/app/grpcweb_spike:test_grpcweb
```
