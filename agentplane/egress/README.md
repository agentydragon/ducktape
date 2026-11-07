# Agentplane egress proxy

The central proxy of Agentplane's credentialless egress: a Go forward proxy that proves each
caller's Pod-bound token or optional Runtime SVID, decides the request from the `egress` rules of `PolicySet`s a
`PolicyBinding` grants it, and substitutes the real credential. What it guarantees is in <SPEC.md>. How the kinds compose into
one decision is in <../docs/egress_composition.md>. Transport choices and rejected alternatives are in
<docs/design.md>.

The CLI bundles the [egress guide](docs/agents/llms.txt) and its credential and refusal guide.
Run `agentplane egress guide` or `agentplane egress guide credentials` inside a Sandbox.

```sh
bbr test //agentplane/egress/...
```

## Layout

One Go package per concern:

- `cmd`: the `egress-proxy` entry point and its settings.
- `proxy`: CONNECT tunnels intercepted with a forged leaf, and plain HTTP proxying, on `net/http`.
  Every request passes identity, policy, credential and address checks before anything is dialed.
  One forward listener on port 8888 accepts an explicit list of hop authentication modes.
- `identity`: the shared `workload_auth` resolver behind the existing token hop, translating a
  refused bearer into the reason the client sees. SVID requests resolve the URI to a live Sandbox
  before using its ServiceAccount as the policy subject.
- `policy`: the pure decision over an in-memory `Index` — the matching egress rule the request's
  placeholder directs it to, and substitution. The bindings and their egress rules are a
  `../policy/reader` index. No I/O.
- `presentation`: one parse per declared target, shared by detection and substitution — where a
  credential's placeholder sits in a request, and how to put the real value there.
- `resources`: the boundary types of `EgressCredential`, the egress rules of a `PolicySet`, and Secrets
  as read off the API server; the derivation of a credential's placeholder from its name.
- `upstream`: the admitted host resolved by the proxy, refused when it points anywhere not globally
  reachable, and pinned so the dial goes to the address checked.
- `informer`: list-and-watch of those kinds into each replica's `Index`. `../policy/reader` watches
  the PolicySets and PolicyBindings, keeps the `EgressReady` condition on each binding, and bounds
  watch periods for every kind; this package adds EgressCredentials and Secrets through
  Kubernetes-generated clients and shared informers from `client/`.
- `rules`: the `EgressRules` gRPC service, which projects the authenticated workload's effective rules.
- `decision` and `history`: admission records, and their bounded best-effort write to the shared
  PostgreSQL history; `admin` serves that history, a change stream, readiness and liveness, and
  those reads again as the gRPC `EgressAdmin` and standard health services.
- `history_migrations`: the Goose schema of that history (below), embedded in the proxy binary.

Also here:

- `sidecar/`: the Go runtime behind image `agentplane-egress-sidecar`. It reads the Pod's
  tagged `proxy.transport.auth` on each HTTP request or CONNECT and forwards it as
  `Proxy-Authorization`. `proxy` contains one `address` and a tagged `transport`: plaintext only
  accepts a required Kubernetes bearer token file; TLS requires its CA file and accepts either
  that bearer token or a Runtime SVID certificate and key. This makes TLS independent from hop
  authentication while keeping SPIFFE authentication from being configured without TLS. The
  sidecar validates the bearer token at startup and rereads it per request. Its projected
  workload-audience token remains separate in `audience_token_files` as destination context; it
  never authenticates the proxy hop.
  It also hosts direct generic gRPC routes to Agentplane services, replacing authorization with
  that service's projected token on every call. The runtime uses no Python interpreter. Its
  settings come only from the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE`, which it
  refuses to start without; it reads no per-setting env and accepts no flags.
- `sidecar_config.py` and `platform_routes.py`: Python settings and route models cdk8s renders each
  Sandbox template's sidecar settings ConfigMap from. The process entry point and both forwarding
  paths live in Go; `platform_routes.py` does not serve traffic.
- `../app/go/streamapi.go`: the integration app's authenticated Egress HTTP facade
  for the generated Egress API.
- `testing/`: the throwaway CAs and process launchers `test_proxy.py` runs the built binaries with.
  It drives the real binaries against envtest API servers (<../testing/kubernetes_apiserver.py>) and
  a PostgreSQL container; the package tests beside the code cover the decision itself.

The versioned Kubernetes types live in `api/v1alpha1/` and `../policy/api/v1alpha1/`.
Their `+genclient` markers drive the pinned Kubernetes `client-gen`, `lister-gen` and `informer-gen`
actions in <../../devinfra/go/kubernetes_clients.bzl>. Generated Go packages are Bazel outputs;
there is no checked-in generated client tree or manual regeneration step. Build them with
`bbr build //agentplane/egress/client:informers //agentplane/policy/client:informers`.

## Running

Settings are YAML keys of the file named by `AGENTPLANE_EGRESS_CONFIG_FILE`. Scalar settings also
accept `AGENTPLANE_EGRESS_*` environment overrides; lists and maps come from the file. The binary
accepts `--migrate-only` for explicit schema initialization using `AGENTPLANE_EGRESS_DATABASE_URL`. Unknown file keys are refused by Viper
(<cmd/config.go>); unrelated environment variables are ignored. The proxy needs an interception CA (`ca_cert`,
`ca_key`) that the runner containers trust; it signs a short-lived leaf per handshake. Upstream certificates are
verified against the system roots, or against `upstream_ca_file` when given; the deployment passes
public roots plus the cluster CA, so a `clusterInternal` rule can reach the API server. Identity and egress policy come from
the API server, in-cluster or through `kubeconfig`.

The proxy embeds history migrations and is published as `agentplane-egress-proxy` (<../../devinfra/ci/image_targets.json>). The Deployment,
sidecar, and CA distribution are generated by `cluster/cdk8s/agentplane/egress.py`. Flux selects
the newest published devel tags through the shared Forgejo ImageRepository/ImagePolicy roster
and each environment's image-pins component. Normal startup initializes the schema before serving or starting decision workers.
The deployment supplies proxy settings through its YAML file and `AGENTPLANE_EGRESS_*` environment.
The deployment has one forward listener and one Service/Pod port. Staging keeps its explicit
token-only, plaintext configuration. `enable_svid_auth=True` on the cdk8s `Egress` construct adds
TLS certificate and trust files and allows both `spiffe` and `kubernetes_bearer` on that listener.
The bearer mode remains available without a client certificate over TLS, while the SPIFFE mode
requires one; a request presenting both credentials or neither is refused before policy evaluation.
The listener uses TLS whenever that mixed configuration is enabled, so bearer clients must use TLS
too. The client certificate must be a current Runtime SVID with explicit client-auth EKU. Every
request resolves its Sandbox UID and accepted credential ID through a live API read; the policy
subject is the Sandbox's own ServiceAccount name. A deleting Sandbox remains authenticated while
its current runner credential is accepted, so the runner can quiesce and seal final Session history.
Clearing or replacing that credential, changing its runner binding, or removing the Sandbox revokes
the SVID. Caller headers and X.509 subject DNs do not set identity.

The sidecar's SVID mode also forwards its separately projected workload-audience token as a
`X-Agentplane-Workload-Token` entry for `projectedWorkloadToken` credentials. The proxy verifies it
against the live Sandbox Pod and the credential's configured audience; it never treats it as a
second hop credential.

The `test_proxy.py` harness exercises the real binary's shared TLS listener against envtest, including
bearer-only TLS, SVID-only TLS, dual-auth refusal, live
Sandbox deletion/replacement, stale credential IDs, projected tokens for another Pod under the same
ServiceAccount, forged subject headers, and expiry after a CONNECT tunnel is open.

## BuildBuddy clients

BuildBuddy uses the same API-key header on its JSON-over-HTTP API and its gRPC services:
`x-buildbuddy-api-key`. For a local Bazel or BuildBuddy client, declare that header as a whole-value
target; gRPC metadata arrives at the proxy as the initial HTTP/2 request headers, so the existing
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

The egress policy still decides the admitted hosts, methods, and paths. `app.buildbuddy.io` serves the
HTTP API; `remote.buildbuddy.io` serves Build Event Service, remote cache, Remote Execution, and the
Remote Runner control service over gRPCS. Standard server-authenticated TLS is sufficient for
API-key authentication. BuildBuddy also supports mTLS as a separate authentication mode; that is
not header substitution, and this proxy does not present a BuildBuddy client certificate.

This support is deliberately **local-client only**. `bb remote` first authenticates its local gRPC
control call with this metadata, but it also copies the API key into the Bazel command executed by
BuildBuddy's hosted runner. That nested Bazel process is outside Agentplane egress, so a placeholder
would remain inert there. Do not configure credentialless `bb remote` until BuildBuddy offers a
runner-side credential reference or Agentplane owns an equivalent broker at that boundary; the
boundary and the candidate rewrite are in <../docs/buildbuddy_remote_auth.md>.

## Projected workload credentials

`authenticatedWorkloadToken` remains supported for bearer-authenticated hops and uses only their
verified bearer. An SVID hop has no such bearer; destinations use explicit audience projections.

For a trusted first-party destination that directly validates the Sandbox Pod's projected workload
token, an `EgressCredential` can name the token audience and exact target. The sidecar presents that
projection to central, which reviews it against the authenticated caller before substitution.
This remains ordinary egress policy and target substitution; a rule must name the credential and the
request must present its placeholder at the declared target.

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: EgressCredential
metadata:
  name: agentplane-workload
spec:
  description: Calling Sandbox Pod workload identity for trusted first-party services.
  source:
    projectedWorkloadToken:
      audience: agentplane-egress
  targets:
    - header: Authorization
      method: schemeToken
      scheme: Bearer
```

The sidecar projects the configured workload audience (`WORKLOAD_TOKEN_AUDIENCE`,
`agentplane-egress`) and sends it under `X-Agentplane-Workload-Token`. Central reviews and binds it
to the authenticated caller, strips the hop headers, and substitutes it only when the selected rule
names this credential and the request presents exactly
`Authorization: Bearer agentplane-credential-agentplane-workload`. Missing, stale, or mismatched
projected context fails closed.

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

`EgressRules.GetRules` is the generated facade behind the authenticated Rules API. Its in-process
channel preserves the API middleware's verified principal and cancellation, without a loopback
socket or forwarded identity metadata. Authentication failure is `401`; unavailable verification
is `503`. The response contains only that ServiceAccount's redacted rules and credential guidance.
