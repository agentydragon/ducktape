# Credential destination transport

**Proposed decision, 2026-10-19; no enforcement change selected.** Decide which upstream schemes
and ports may receive a substituted credential. This applies to the central proxy's Secret,
authenticated-workload-token, and projected-workload-token sources alike.

## Current boundary

`policy.Request` carries method, host, path, headers, and body, but no scheme or port
(`policy/evaluate.go`). `proxy/gate.go` passes the port to address admission independently of
credential selection. `proxy/proxy.go` distinguishes HTTP, HTTPS, and cleartext HTTP/2, then dials
the admitted address. Its TLS transport verifies upstream certificates, but a matching rule can
also substitute the same credential into plaintext HTTP. The HTTP/2 transport test deliberately
exercises that behavior with a local upstream.

`clusterInternal` permits private DNS answers; it is not a transport-security declaration.
Kubernetes already combines that flag with an HTTPS endpoint. Public Gateway TLS also says
nothing about encryption between the Gateway and its backend. Direct sidecar platform routes
are a separate transport boundary, owned by the existing platform-routing decision.

## Source inventory

Audited the committed testing/staging PolicySets, including delivery and public-coder grants, and
their cdk8s authors at `b3ec6c38b6`. This is desired configuration, not a live traffic inventory.
The table's schemes and ports are the proposed permitted origins; current rules do not restrict
either. Retain existing host, method, path, and identity restrictions.

| Credentials and destinations                                                                          | Proposed origins                 | Authoritative source                                                                       |
| ----------------------------------------------------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------ |
| `github-pat`: GitHub API, Git, codeload, and GitHub content hosts; `buildbuddy`: app and remote hosts | HTTPS 443                        | `cluster/cdk8s/agentplane/egress.py`                                                       |
| `google-readonly`, `grocy-sf-readonly`, `activitywatch-read`, public `aiquota-read`, `haku-mailbox`   | HTTPS 443                        | `cluster/cdk8s/agentplane/egress_staging_credentials.py` and `actions_staging_policies.py` |
| Public `forgejo-haku` and `forgejo-finance-agent`: `git.allegedly.works`                              | HTTPS 443                        | `egress_staging_credentials.py::_forgejo_rules`                                            |
| `brave-search`, `public-coder-matrix`, plus reused GitHub/AIQuota credentials                         | HTTPS 443                        | `cluster/cdk8s/public_coder/egress.py`                                                     |
| `agentplane-workload`: LLM ingress and Actions; `agentplane-notifications-workload`: Notifications    | HTTP 8080                        | `cluster/cdk8s/agentplane/{egress,llm_ingress,actions,notifications}.py`                   |
| `agentplane-workload`: egress rules/discovery API                                                     | HTTP 80                          | `egress.py::agent_api`                                                                     |
| `agentplane-runner-ingress`: History ingress                                                          | HTTP/h2c 8081                    | `history_service.py::ingress_service`                                                      |
| `kubernetes-workload`: `kubernetes.default.svc.cluster.local`                                         | HTTPS 443                        | `egress.py`, `sandbox_pod.py`                                                              |
| `ollama`, `litellm-cheap-experiments`                                                                 | HTTP 11435 and 4000 respectively | `egress.py::_egress_policies`, Ollama and LiteLLM Service definitions                      |
| Internal Forgejo credentials and existing host aliases                                                | HTTP 3000                        | `egress_staging_credentials.py::_forgejo_rules`, `cluster/cdk8s/forgejo/app.py`            |
| `home-assistant-readonly`; internal `aiquota-read`                                                    | HTTP 8123 and 8080 respectively  | `home_assistant_read_egress_rules`, `cluster/cdk8s/aiquota.py`                             |
| `clickhouse-aiquota-history-credentials`, `public-coder-clickhouse`                                   | HTTP 8123                        | `cluster/cdk8s/clickhouse/client.py` and the granting PolicySets                           |
| `plaid-pgweb`                                                                                         | HTTP 80                          | `cluster/cdk8s/plaid_mcp/pgweb.py` and `cluster/cdk8s/agent_access_profiles.py`            |

## Alternatives and recommendation

1. Require TLS globally for substitution and maintain a separate deployment allowlist of
   plaintext exceptions. This is small at the gate, but splits each credential grant across
   Kubernetes policy and deployment configuration; changing a grant can require rolling proxies.
2. Add explicit upstream `(scheme, port)` pairs to credential-bearing egress rules. The grant
   then names where a credential may travel, and watch updates/revocation use the existing policy
   path. This expands the Rule schema and its Go/Python consumers.
3. Treat `clusterInternal` as permission to substitute over HTTP. This avoids another field but
   grants more than its current private-address meaning, including HTTP to Kubernetes and every
   port on a matched host. It cannot express the inventory's distinctions.

Recommend **2**, with HTTPS 443 for the public destinations above and explicit HTTP/port pairs
for reviewed internal routes. Permit nonstandard HTTPS ports only when explicitly granted.
Keep plaintext exceptions on exact internal hostnames and ports with `clusterInternal: true`;
do not infer them from a suffix, DNS resolution, credential source, or that flag alone. Preserve
the existing narrower paths and methods on those grants. An origin-pair list avoids accidentally
granting the cross-product of independent scheme and port lists.

For review: choose the rule-local origin-pair model versus the deployment allowlist, and approve
the explicit internal plaintext set. This proposal does not select a routing change, move
credentials into the sidecar, or migrate internal services to TLS. Runner control uses a separate
mTLS Gateway listener and is outside this bearer-substitution inventory.

## Enforcement and implementation acceptance

- Require explicit origin pairs on credential-bearing rules; omission must not retain unrestricted
  substitution. Credentialless rules retain their current HTTP(S) behavior. A credentialless
  grant must never rescue a refused placeholder-bearing request.
- Decide CONNECT using its actual host and port, then decide every inner request using its own
  admitted authority, normalized scheme and effective port. CONNECT permission is not permission
  to substitute. Derive scheme from the proxy's transport handling, never a forwarded header.
  HTTP/2 versus HTTP/1.1 does not change the origin contract; a WebSocket upgrade is subject to
  the same admission before switching protocols.
- Check transport permission before returning credential material or opening the upstream
  connection, and include it in the existing policy recheck after DNS I/O. Preserve verified TLS,
  pinned-address dialing, private-address checks, and per-request identity/revocation semantics.
- Update Go API markers/generated CRDs, runtime parsing, cdk8s authors, and all callers together.
  Render every credential grant from the audited Service/origin definitions; do not maintain a
  second independent port inventory in proxy code. Publish the selected contract in `SPEC.md`.
- Use the existing real-binary tests for a permitted TLS rewrite, refused public plaintext with
  zero upstream accepts, an explicit internal h2c rewrite, refusal on another port, and watch
  revocation. Keep the existing streaming suite; no separate protocol-by-credential matrix.

Before rollout, review the generated grants against this inventory and account for existing
bindings. Old proxy images ignore the new restriction, so publication alone is not enforcement:
all serving replicas must run the enforcing reader with the new grants installed. The existing
automated tests establish behavior; any live check should target a specific rollout uncertainty.
