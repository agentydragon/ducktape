# Egress proxy design decisions

Choices a competent author would reopen, with the constraint that decided each.

## Transport: the standard library, not GoProxy

`net/http` does the CONNECT hijack, one server per tunnel (ALPN picks h2 or HTTP/1.1; cleartext
picks h1 or prior-knowledge h2c) and one `httputil.ReverseProxy` per admitted request.
`elazarl/goproxy` v1.9.2 was tried first and fails the contract that matters for gRPC:

- Its h2 MITM handler calls `WriteHeader`, then blocks reading the first upstream body bytes.
  `net/http` writes the HEADERS frame on the first body write or `Flush`, so an upstream that
  sends headers and waits for a client message never delivers initial metadata. `ReverseProxy`
  flushes headers on a timer started before the first body read.
- Its defaults skip upstream TLS verification and honor proxy environment variables, both
  unacceptable at a credential boundary. A four-line patch fixes the flush; the rest is replaced
  either way.

Consequences the standard library transport owns: hijacked CONNECT connections sit outside
`http.Server` shutdown, so the proxy tracks tunnels itself to drain; the upstream transport is
single-use per admitted request (`DisableKeepAlives`, which covers HTTP/2 too), so a pooled or
coalesced connection can never carry a request to an address other than the one the admission
pinned.

## Test tiers

A real kube-apiserver and etcd (envtest) check what the permissive fake API server cannot: CRD
validation and list/watch/delete semantics. TokenReview stays a controlled dependency, so bound
ServiceAccount token issuance, signatures and Pod replacement are covered only by the Kubernetes
integration tier.
