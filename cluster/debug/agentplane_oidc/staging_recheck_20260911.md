# Staging federation recheck, 2026-09-11 UTC

Read-only checks following intermittent browser `/push/config` HTTP 403 with
`operator_federation_exchange_failed`. No network or DNS configuration changed.

- App image: `devel-20260911020842-b52faa5`, pod
  `agentplane-app-65dc8d49b8-mkvbb`, IP `10.244.2.179`, node `ovh-ns103711`.
- Live app CNP generation 10 and Actions generation 6 retain host/remote-node
  TCP 443 with Authentik SNI and selected Authentik backend TCP 9000 with the
  same SNI. Authentik ingress generation 7 retains staging namespace ingress.
- Git provenance: #5932 / a3219ec716 (Authentik ingress), #6007 / b11b4a837a
  (app backend egress), #6012 / ddc21cab93 (Actions backend egress).
- Credential-free JWKS GETs from the app using its bundled HTTPX client and
  normal DNS: eight HTTP 200, four ConnectError in twelve bounded requests.
- Verified TLS with fixed destination IP and canonical SNI/Host: all four
  remote node IPs returned HTTP 200 twice each; local node `147.135.39.176`
  reset TLS twice (errno 104). Gateway Service `10.106.122.5` returned 200
  three times. Certificate verification stayed enabled using bundled certifi.
- Logs include successful real push config and subscription GETs at
  approximately 02:27 UTC. No claim that those prove browser registration.

This reproduces the local-node failure already recorded in
`local_gateway_tls.md`, not the missing backend CNP fixed earlier. The precise
exception for the reported browser request is unavailable: federation catches
HTTP, OAuth, signing-key availability, and ValueError under one public code
without logging the cause. Transport failures can therefore become HTTP 403.

Do not apply a cluster-wide Authentik DNS rewrite: `service_dns_scope.md`
documents lack of Gateway Service reachability on non-Gateway nodes. A scoped
routing change or datapath repair needs separate review and verification.

The supplied browser VM225 `reportAllChanges/startTime` exception has no matching
symbol in the Agentplane frontend source or dependency manifests searched.
Its script source is needed before attributing it to the application or an
extension; the stack alone does not identify its owner.
