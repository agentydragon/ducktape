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

## Sidecar settings: protobuf maps preserve data keys

Python authors and the Go sidecar share `sidecar/configpb/config.proto`. `protoyaml` preserves
case in the maps keyed by data: `audience_token_files` by audience and `platform_routes` by route name.
Viper lowercases every key, map keys included. With v1.21.0 (`ExperimentalBindStruct`,
`UnmarshalExact`) two audiences that differ only in case merge into one entry and a route name loses
its case, so a Pod could offer one audience the other's token. `config_test.go` keeps both spellings
apart. A move to Viper first needs those maps to become lists of records.

## Test tiers

A real kube-apiserver and etcd (envtest) check CRD validation and list/watch/delete semantics.
The transport tests use real Pod-bound, audience-scoped ServiceAccount tokens and TokenReview.
Scripted API servers remain for controlled replica watch faults and process lifecycle scenarios.
