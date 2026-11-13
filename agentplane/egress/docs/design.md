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
`http.Server` shutdown, so the proxy tracks tunnels itself to drain. `http.Transport` pools by the
URL's host, not by the address an admission pinned, so the proxy keeps one transport per pin
(admitted authority, pinned address, scheme and protocol) in a bounded LRU: a pooled connection
only carries requests whose admission pinned that same address, and a host that re-resolves gets a
new pool while the old one idles out. Single-use connections would hold the same invariant, at a
TCP and TLS handshake per request.

## Sidecar relay: a per-connection request loop, not `httputil.ReverseProxy`

The sidecar parses each request with `http.ReadRequest` and writes it with `Request.WriteProxy` over
one central proxy connection per workload connection. `http.Server` with `ReverseProxy` would pool
upstream connections, breaking the per-client-connection `ConnectionID` the central proxy records;
rewrite responses (`Date`, sniffed `Content-Type`, re-chunking, hop-by-hop stripping); and answer
malformed requests without the `x-agentplane-egress-sidecar` refusal header the `agentplane` CLI
classifies errors by. CONNECT needs a hijacked tunnel either way.

## Settings: a required YAML file, not Viper

Both Go proxy processes read one required YAML settings file and reject unknown keys and extra
documents. The main proxy decodes YAML through `mapstructure` with its existing listener decode
hooks, keeping its structured authentication settings in sync with the application model. The
sidecar uses `yaml.v3` directly because its `platform_routes.routes` map is keyed by route name; each
route also allowlists exact gRPC service prefixes. Viper lowercases every key, map keys included.
With v1.21.0 (`ExperimentalBindStruct`, `UnmarshalExact`) two audiences that differ only in case
merge into one entry and a route name loses its case, so a Pod could offer one audience the other's
token. `config_test.go` keeps both spellings apart. Static settings have no per-setting environment
or CLI overrides; the proxy database URL remains an explicit Secret-backed environment input.

## Test tiers

A real kube-apiserver and etcd (envtest) check CRD validation and list/watch/delete semantics.
The admin gRPC tests use real Pod-bound, audience-scoped ServiceAccount tokens and TokenReview.
Scripted API servers remain for controlled replica watch faults and process lifecycle scenarios.
