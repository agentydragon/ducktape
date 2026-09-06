# Agentplane egress proxy

The central proxy of Agentplane's credentialless egress: a mitmproxy addon that proves each
caller's Pod-bound token, decides the request from `EgressPolicy` and `EgressBinding` resources, and
substitutes the real credential. What it guarantees is in <SPEC.md>. How the two kinds compose into
one decision is in <../docs/egress_composition.md>.

```sh
bbr test //x/agentplane/egress/...
```

## Layout

- `resources.py`: the boundary models of the three kinds, Sandboxes, and Secrets as read off the
  API server; the derivation of a credential's placeholder from its name.
- `presentation.py`: one parse per declared target, shared by detection and substitution — where a
  credential's placeholder sits in a request, and how to put the real value there.
- `policy.py`: the pure decision over an in-memory `Index` — subject bindings, the matching rule
  the request's placeholder directs it to, substitution, binding status. No I/O.
- `identity.py`: the shared `sandbox_auth` TokenReview/live-owner resolver plus the egress-only
  source-Pod address check and expiry-bounded verdict cache.
- `upstream.py`: the admitted host resolved by the proxy, refused when it points anywhere not
  globally reachable, and pinned so the dial goes to the address checked.
- `informer.py`: list-and-watch of the five kinds into the `Index`, and the binding status
  writes.
- `rules_api.py`: the agent-facing
  `agentplane-egress.agentplane-staging.svc.cluster.local/v1/rules` API and the narrow
  Sandbox-name/UID-to-redacted-projection boundary; `addon.py` is only its current authenticated
  proxy transport as well as the mitmproxy gate for ordinary egress. `decisions.py` is the ring and
  JSON log line; `admin.py` the `/decisions` and `/healthz` listener.
- `proxy.py`: mitmproxy hosted in-process with the fail-closed options pinned; `main.py` the
  entry point and its `Settings` (`--flags` and `AGENTPLANE_EGRESS_*`).
- `sidecar.py`: the per-sandbox relay, image `agentplane-egress-sidecar`: reads the Pod's
  projected token per request, adds it as `Proxy-Authorization`, and forwards to the proxy.
- `testing/`: the fake API server and the throwaway CAs the tests run against.

## Running

The proxy needs an interception CA (`--ca-cert`, `--ca-key`) that the runner containers trust
and a writable `--confdir` where mitmproxy keeps it and the leaves it issues; upstream
certificates are verified against the image's system trust store. Identity and policy come from
the API server, in-cluster or through `--kubeconfig`.

The image `//x/agentplane/egress:image` is published as `agentplane-egress`
(<../../../devinfra/ci/image_targets.json>); the Deployment, sidecar, and CA distribution are the
cluster manifests' concern (`cluster/k8s/agentplane-staging`).

## BuildBuddy clients

BuildBuddy uses the same API-key header on its JSON-over-HTTP API and its gRPC services:
`x-buildbuddy-api-key`. For a local Bazel or BuildBuddy client, declare that header as a whole-value
target; gRPC metadata is exposed to the addon as the initial HTTP/2 request headers, so the existing
target model substitutes it without a gRPC-specific credential kind.

Interactive browser authentication is separate: BuildBuddy's login flow establishes HTTP-only
`Authorization`, `Authorization-Issuer`, and `Session-ID` cookies. A browser session should keep
using those cookies; the API key is for programmatic HTTP/gRPC calls, not a cookie value the proxy
should synthesize.

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: EgressCredential
metadata:
  name: buildbuddy-local-client
spec:
  description: A scoped BuildBuddy API key for local HTTP API and Bazel remote-protocol calls.
  source:
    secretRef:
      name: buildbuddy-local-client
      key: api-key
  targets:
    - header: x-buildbuddy-api-key
      method: wholeValue
```

The policy still decides the admitted hosts, methods, and paths. `app.buildbuddy.io` serves the
HTTP API; `remote.buildbuddy.io` serves Build Event Service, remote cache, Remote Execution, and the
Remote Runner control service over gRPCS. Standard server-authenticated TLS is sufficient for
API-key authentication. BuildBuddy also supports mTLS as a separate authentication mode; that is
not header substitution and this proxy does not provision or present a client certificate.

This support is deliberately **local-client only**. `bb remote` first authenticates its local gRPC
control call with this metadata, but it also copies the API key into the Bazel command executed by
BuildBuddy's hosted runner. That nested Bazel process is outside Agentplane egress, so a placeholder
would remain inert there. Do not configure credentialless `bb remote` until BuildBuddy offers a
runner-side credential reference or Agentplane owns an equivalent broker at that boundary.

## Authenticated workload credentials

For a trusted first-party destination that directly validates the Sandbox Pod's projected workload
token, an `EgressCredential` may resolve the bearer already authenticated on the sidecar-to-central
hop. This remains ordinary policy and target substitution: there is no destination-specific branch,
second token, sideband, or unconditional `Authorization` injection.

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: EgressCredential
metadata:
  name: agentplane-workload
spec:
  description: Calling Sandbox Pod workload identity for trusted first-party services.
  source:
    authenticatedWorkloadToken: {}
  targets:
    - header: Authorization
      method: schemeToken
      scheme: Bearer
```

The sidecar still presents the existing configured workload audience (`agentplane-egress` in the
current deployment). Central strips `Proxy-Authorization`, validates and binds its bearer to the
live Sandbox, and substitutes it only when the selected rule names this credential and the request
presents exactly `Authorization: Bearer agentplane-credential-agentplane-workload`. Missing, stale,
or mismatched authenticated context fails closed. Audience migration is a separate deployment
change, not part of this source.

Authoritative evidence is pinned to BuildBuddy source commit
[`6fc01488`](https://github.com/buildbuddy-io/buildbuddy/tree/6fc01488a60d69832f86eff154ac985e1170653e):
the [authentication guide](https://github.com/buildbuddy-io/buildbuddy/blob/6fc01488a60d69832f86eff154ac985e1170653e/docs/guide-auth.md)
names the gRPC metadata header and TLS modes; the
[HTTP API documentation](https://github.com/buildbuddy-io/buildbuddy/blob/6fc01488a60d69832f86eff154ac985e1170653e/docs/enterprise-api.md)
uses the same header; the [browser cookie definitions](https://github.com/buildbuddy-io/buildbuddy/blob/6fc01488a60d69832f86eff154ac985e1170653e/server/util/cookie/cookie.go)
show the interactive-session form; and the
[`bb remote` implementation](https://github.com/buildbuddy-io/buildbuddy/blob/6fc01488a60d69832f86eff154ac985e1170653e/cli/remotebazel/remotebazel.go)
both appends the key to the local outgoing gRPC context and retains it in the nested Bazel command.

## Rules API

Agents send an ordinary proxied HTTP `GET` to
`http://agentplane-egress.agentplane-staging.svc.cluster.local/v1/rules` with
`Authorization: Bearer agentplane-credential-agentplane-workload`. The placeholder is inert and
published in nonsecret runner instructions. The default `egress-rules` policy binds this exact
host, method, and path to the existing `agentplane-workload` credential's `schemeToken` target.
Central applies normal exact-placeholder substitution using the authenticated sidecar workload
context; no rules-specific proxy dispatch or credential injection mode is involved.

Service port `80` targets the separate HTTP API listener on `8082`; port `8888` remains the forward
proxy. Central resolves and dials the API like any other cluster-internal policy destination.
The FastAPI endpoint independently validates ordinary `Authorization` through
`SandboxPrincipalAuthenticator` (TokenReview and live Pod/Sandbox resolution). The API sees central's
source address, not the Sandbox Pod address; proxy-hop identity and caller metadata are not API
identity authorities. Missing or forged destination auth fails closed.

The API shares the central process's current enforcement `Index` through `RulesProjection`, which
checks the authenticated Sandbox UID and returns only the redacted field allowlist. Operator
`/decisions` and `/healthz` remain on the separate admin listener, not the rules API. Network policy
admits the agent API from central egress only. Service target-port separation prevents recursion.

## ServiceAccount permissions

In the sandbox namespace: `get`, `list`, `watch` on `egresspolicies`, `egressbindings`,
`egresscredentials` and `sandboxes.agents.x-k8s.io`; `get` on `pods`; `patch` on `egressbindings/status`. In the
credentials namespace (`--credentials-namespace`, `agentplane-egress-credentials` by default):
`get`, `list`, `watch` on `secrets`, and nothing in the sandbox namespace. Cluster-wide: `create`
on `tokenreviews.authentication.k8s.io`. The `EgressBinding` CRD must enable the `status`
subresource, which the status writes go through.

Substituted credentials live in a namespace of their own because RBAC cannot filter Secrets by
label: a namespace-wide read in the sandbox namespace would hand the proxy the model key and the
database credential along with the ones it is meant to substitute.

## Open questions

- **Whether an agent also reads its own recent decisions.** The ring already answers "why was I
  denied", and a failure the agent can diagnose itself is the practical win; nothing serves it to
  the agent-facing surface today.
- **Whether that surface versions separately from the operator API.** Agents are long-lived and
  roll independently of the app, so the two may not be able to move together for long.
