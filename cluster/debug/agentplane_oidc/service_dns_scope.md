# Authentik internal DNS scope checks — 2026-09-10

Read-only checks, approximately 04:30–04:32 UTC. No DNS or cluster changes.

## Non-Gateway Service reachability

Gateway CiliumEnvoyConfig selects region=hil. Live nodes wyrm2 (proxmox) and
optiplex (home) do not match. Both Cilium service maps contain
10.106.122.5:443 with no backends; neither has a listening TCP 80/443 socket.
Host-side verified-TLS attempts to the Service fail with errno 113.

Pod network-namespace controls:

| Pod                                              | Node     | Service IP TLS | Public node 147.135.37.175 TLS |
| ------------------------------------------------ | -------- | -------------- | ------------------------------ |
| airlock-7cbd44f584-2b5dd, 10.244.5.250           | wyrm2    | errno 113      | verified TLS 1.3               |
| grafana-deployment-7d8f85d6fb-dtj74, 10.244.8.94 | optiplex | errno 113      | verified TLS 1.3               |

Airlock public issuer discovery through the public-node control returned HTTP
200 at 04:31:36 UTC. All TLS probes used canonical SNI and hostname validation.
No credentials or cookies were sent; no response bodies or headers beyond
HTTP status were recorded.

## Client inventory

Configured server-side public-issuer clients active on wyrm2 include aiquota,
Airlock, Manifold MCP, Plaid DB MCP, Postscanmail MCP, and Tana MCP facade. They
have unrestricted egress or ingress-only policies. Changing shared CoreDNS to
this Gateway Service would therefore break their connection path independently
of policy authorization. Inventory establishes configured use and placement,
not credentialed functional success for each application.

Other configured consumers on OVH/Gateway nodes include Agentplane app/Actions,
Haku console, Forgejo, Grocy MCPs, Langfuse, study-casino, and Gatus. App/Actions
have the new backend:9000 canonical-SNI rules. Others have no restrictive
egress selection, or Gatus explicit allow-all.

Home Assistant on optiplex has hostNetwork with ClusterFirst, so actual DNS
resolver use needs separate verification. Recent Authentik JWT rotation Jobs
on optiplex also use the canonical token endpoint; scheduled consumers must be
included in any migration. No Secret contents were inspected.

Grafana is only a transport control here: its public auth_url is a browser
redirect; token/userinfo already target the internal Authentik Service.
Haku static frontend AUTH_ORIGIN is also not an OIDC backchannel. Kubectl MCP
authorization metadata alone was not treated as proof of server-side traffic.

## Separate policy risk

Public-coder-agent-proxy and agentplane-egress allow public/node HTTPS but lack
Authenti​​k backend:9000 permission. A DNS cutover could turn an otherwise allowed
request through those proxies into a Service-path policy denial. No existing
Authenti​​k request through those proxies was demonstrated; do not widen their
policy based on hypothetical use alone.

## Conclusion

Do not apply a cluster-wide auth.allegedly.works rewrite to the current Service.
The Service path is verified for app/Actions on Gateway nodes, but not portable
across this cluster's current placement. Before shared DNS changes, establish a
frontend reachable from non-Gateway nodes and audit remaining client policies.
Alternatively evaluate explicitly workload-scoped routing for app/Actions,
including scheduling and Service-IP lifecycle ownership. No such change was
made or validated here. No BuildBuddy invocation: these were live read-only
network diagnostics, not code validation or application acceptance.
