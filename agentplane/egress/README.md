# Agentplane egress proxy

The central proxy of Agentplane's credentialless egress: a Go forward proxy that proves each
caller's Pod-bound token, decides the request from the `egress` rules of `PolicySet`s a
`PolicyBinding` grants it, and substitutes the real credential. What it guarantees is in <SPEC.md>. How the kinds compose into
one decision is in <../docs/egress_composition.md>. Transport choices and rejected alternatives are in
<docs/design.md>.

The rules API serves [`/llms.txt`](docs/agents/llms.txt), its linked credential/refusal guide under
`/docs/agents/` and the OpenAPI document of `/v1/rules` at `/openapi.json`. The document is generated from
[`egress_rules.proto`](egress_rules.proto), so a field is described once, in the proto. Update the
guide when credential presentation or refusal behavior changes.

```sh
bbr test //agentplane/egress/...
```

## Layout

One Go package per concern:

- `cmd`: the `egress-proxy` entry point and its settings.
- `proxy`: CONNECT tunnels intercepted with a forged leaf, and plain HTTP proxying, on `net/http`.
  Every request passes identity, policy, credential and address checks before anything is dialed.
- `identity`: the shared `workload_auth` resolver behind a proxied connection, translating a refused
  bearer into the reason the client sees.
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
- `rules`: the agent-facing `egress.agentplane-staging.svc.cluster.local/v1/rules` API, on its own
  independently authenticated listener: the generated HTTP/JSON facade of the gRPC `EgressRules` service,
  beside the agent documentation.
- `decision` and `history`: admission records, and their bounded best-effort write to the shared
  PostgreSQL history; `admin` serves that history, a change stream, readiness and liveness, and
  those reads again as the gRPC `EgressAdmin` and standard health services.
- `history_migrations`: the Goose schema of that history (below), embedded in the proxy binary.

Also here:

- `sidecar/`: the Go runtime behind image `agentplane-egress-sidecar`. It reads the Pod's
  projected token for every HTTP request or CONNECT and forwards it as `Proxy-Authorization`.
  It also hosts direct generic gRPC routes to Agentplane services, replacing authorization with
  that service's projected token on every call. The runtime uses no Python interpreter. Its
  settings come only from the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE`, which it
  refuses to start without; it reads no per-setting env and accepts no flags.
- `sidecar_config.py` and `platform_routes.py`: Python settings and route models cdk8s renders each
  Sandbox template's sidecar settings ConfigMap from. The process entry point and both forwarding
  paths live in Go; `platform_routes.py` does not serve traffic.
- `../app/go/streamapi.go`: the integration app's authenticated `EgressAdmin.WatchDecisions` client
  and browser stream projection. `decisions.py` remains the Python value model used by system tests.
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
not header substitution and this proxy does not provision or present a client certificate.

This support is deliberately **local-client only**. `bb remote` first authenticates its local gRPC
control call with this metadata, but it also copies the API key into the Bazel command executed by
BuildBuddy's hosted runner. That nested Bazel process is outside Agentplane egress, so a placeholder
would remain inert there. Do not configure credentialless `bb remote` until BuildBuddy offers a
runner-side credential reference or Agentplane owns an equivalent broker at that boundary; the
boundary and the candidate rewrite are in <../docs/buildbuddy_remote_auth.md>.

## Authenticated workload credentials

For a trusted first-party destination that directly validates the Sandbox Pod's projected workload
token, an `EgressCredential` may resolve the bearer already authenticated on the sidecar-to-central
hop. This remains ordinary egress policy and target substitution: there is no destination-specific branch,
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

The sidecar presents the configured workload audience (`WORKLOAD_TOKEN_AUDIENCE`,
`agentplane-egress`). Central strips `Proxy-Authorization`, validates and binds its bearer to the
authenticated caller, and substitutes it only when the selected rule names this credential and the request
presents exactly `Authorization: Bearer agentplane-credential-agentplane-workload`. Missing, stale,
or mismatched authenticated context fails closed.

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
`http://egress.agentplane-staging.svc.cluster.local/v1/rules` with
`Authorization: Bearer agentplane-credential-agentplane-workload`. The placeholder is inert and
published in nonsecret runner instructions. The default `basic` egress policy binds this exact
host, method, and path to the `agentplane-workload` credential's `schemeToken` target.
Central applies normal exact-placeholder substitution using the authenticated sidecar workload
context; no rules-specific proxy dispatch or credential injection mode is involved.

Service port `80` targets the separate HTTP API listener on `8082`; port `8888` remains the forward
proxy. Central resolves and dials the API like any other destination admitted by an egress policy.
The endpoint validates ordinary `Authorization` through the same `workload_auth` resolver,
reviewing the bearer itself rather than inheriting the tunnel's verdict: one proxied read is two
reviews, deliberately, each reused for 30 s. The API sees central's
source address, not the caller's Pod address; proxy-hop identity and caller metadata are not API
identity authorities. Missing or forged destination auth fails closed.

The API shares the central process's current enforcement `Index`, which projects the subject the
bearer proved and returns only the redacted field allowlist. Operator
Operator decision and health reads are on the separate admin listener ([Admin gRPC API](#admin-grpc-api)), not the rules API.
Network policy admits the agent API from central egress only. Service target-port separation prevents recursion.
The Service may select a different replica from the one that admitted the request. The answer is
an informational snapshot of the answering replica, not a global acknowledgement or admission promise.

## Admin gRPC API

`EgressAdmin` (<egress_admin.proto>) serves the operator reads on the admin listener:
`ListDecisions` lists a subject's recent decisions, `WatchDecisions` sends that list on subscribing and
again each time it differs, and `GetHealth` carries the readiness verdict as `ready` with the facts it
rests on (`decision_history`, the refresh ages). All three read through the same `Decisions`,
`Changes`, history counters and `Index`.

The standard `grpc.health.v1.Health` service is served on the same listener for Kubernetes' native `grpc` probes:
the empty service is readiness and the service `livez` liveness, which stays `SERVING` while the process
answers so a recoverable watch outage never restarts it. It admits every caller, because the kubelet
sends no bearer; `GetHealth` does not. `Watch` is not served.

The admin listener serves gRPC alone. On shutdown it ends the open watches, then stops gracefully: the
other calls in flight finish within the drain deadline.

Every `EgressAdmin` call admits only the ServiceAccounts in `admin_grpc_callers`, each proving itself by
a bearer of audience `admin_grpc_audience` that is reviewed for every call (an acceptance is reused
for 30 s), before anything is read
(<../docs/grpc_services.md>). A watch is admitted once, so it ends after ten minutes and when the proxy
drains; the caller subscribes again and is admitted afresh.

The health service is always mounted. `EgressAdmin` is mounted only when `admin_grpc_audience` and
`admin_grpc_callers` (`namespace/name` entries) are both set; without them the admin listener serves
health alone.

## Rules gRPC API

`EgressRules` (<egress_rules.proto>) serves `GetRules` on a loopback listener of the process's own; nothing
outside the process calls it. `GET /v1/rules` on the agent API listener is its gRPC-Gateway facade
(<../grpc/facade/facade.go>), which forwards to that listener so the servicer authenticates every request. The answer is the redacted
view of the ServiceAccount the bearer proves and nothing else: the bearer is read from `authorization`
metadata, which the facade fills from the `Authorization` header, and reviewed by the verifier, so the hop
token and the projected tokens it accepts are accepted on both.

**Deviation** from <../docs/grpc_services.md>: callers are the sandbox workloads themselves, so there is no
allow-list and no `auth.Resolver` of its own; every bearer is `Unauthenticated` (`401`) alike, and one that
could not be reviewed is `Unavailable` (`503`). The documentation routes (`/llms.txt`, `/docs/agents/`, `/openapi.json`)
stay hand-written HTTP. It is mounted unconditionally, because it has no setting.

## ServiceAccount permissions

In the rules namespace (`--rules-namespace`): `get`, `list`, `watch` on `egresscredentials`, `policysets` and `policybindings`, and `patch` on
`policybindings/status`. **Nothing at all in an `allowed_service_account_namespaces` entry**: the
TokenReview names the ServiceAccount a bearer's Pod runs as, and that is the subject, so naming a
second workload namespace needs no Role there. In the credentials namespace
(required `--credentials-namespace`): `get`, `list`, `watch` on
`secrets`, and nothing else. Cluster-wide: `create` on `tokenreviews.authentication.k8s.io`. There
is no leader election: every replica applies the same `EgressReady` under one field manager, and
writes only when the condition it decides differs from the one the binding carries.

Substituted credentials live in a namespace of their own because RBAC cannot filter Secrets by
label: a namespace-wide read where the sandboxes run would hand the proxy the model key and the
database credential along with the ones it is meant to substitute. Staging uses
`agentplane-staging-egress-credentials`.

### Rejected: binding a bearer to its Pod's source address

Reading the calling Pod and refusing a connection whose source is not that Pod's IP, on every
request including cache hits, stops a token copied out of its Pod from being replayed elsewhere in
the cluster. It costs a `pods` read in every `allowed_service_account_namespaces` entry, the grant
the rule above keeps at nothing. Without it the exposure is bounded (<SPEC.md> § Identity): one
in-cluster workload borrowing another's egress rules, not a way past the egress policy. Restore it if that
borrowing becomes a real concern — a compromised sidecar reading another Pod's projected token, or
a namespace whose Pod specs are not ours — with a pod IP on the principal, the check in `identity`,
the peer-address read in `proxy`, and that `pods` read.

## Decision history

`history.Log.Record` puts an immutable `decision.Record` on a bounded queue (2,000 by default,
`decision_queue_size`) and returns; admission never waits for the database, and a record that does
not fit is counted and dropped. One writer drains up to 100 records per batch (`decision_batch_size`),
waking at least once a minute when idle, over a pool of two connections whose operations, connection
waits and connects are each bounded to two seconds. A batch gets three attempts with exponential
backoff from one second; a retry reuses the batch's event IDs and decision timestamps, and the insert
is `ON CONFLICT (event_id) DO NOTHING`. Records already past the retention window are dropped before
the write and counted as expired. Shutdown flushes for five seconds and counts what remains as
shutdown loss. `GetHealth` reports the counters as `decision_history`.

After every batch or idle wake the writer deletes at most 1,000 expired rows, selected `FOR UPDATE
SKIP LOCKED` on the `(decided_at, event_id)` index so concurrent replicas neither block nor
contend. Indexed deletion is used rather than time partitions: no measured volume justifies
partition management, and event-ID idempotence stays global. Monitor database size and
`cleanup_failures`; sustained ingestion beyond cleanup capacity is the signal to revisit that.

The API's `at` field is the `decided_at` column; `ingested_at` is the row's insertion timestamp.
`Store.Recent` takes the newest `decision_history_size` rows (200 by default, at most
1,000) inside the retention window and returns them oldest first.

The schema is Goose SQL in `history_migrations/history/`, tracked by `egress_goose_version`.
Staging and testing each hold an `egress` database in their CNPG cluster. Both use Goose;
Alembic is no longer a migration owner. Existing history rows and
the inert `egress_alembic_version` table remain in place. The proxy migrates before starting its
listeners or workers, so a failed migration keeps that Pod unready while previous proxies keep enforcing.

## Open questions

- **Whether an agent also reads its own recent decisions.** Shared history answers "why was I
  denied", and a failure the agent can diagnose itself is the practical win; nothing serves it to
  the agent-facing surface today.
- **Whether that surface versions separately from the operator API.** Agents are long-lived and
  roll independently of the app, so the two may not be able to move together for long.

## Replica lifecycle

Readiness (the empty health service and `GetHealth`'s `ready`) and every CONNECT/HTTP admission share `Index` availability: all four watched kinds must be
initially synced, and none may be failing. A kind fails when one of its list or watch requests fails
or client-go's watch error handler reports an abnormal end, and recovers on its next successful list
or watch; `GetHealth` names each failing kind with its error and since when. client-go owns the
watch timeouts, resumes and relists, and its HTTP/2 health checks turn a dead connection into a
failed request. DB health does not participate. The `livez` health service only proves the admin listener answers; a recoverable watch outage does not
cause a restart loop. Every request on an existing TLS/HTTP2 connection is gated again. After DNS I/O,
a changed egress-policy decision denies that admission without forwarding or replay.
Revocation is eventually observed independently by each watch, not globally linearizable.

SIGTERM/SIGINT marks the index draining, so readiness fails and the listener closes at once.
Admitted requests (HTTP responses, WebSockets, gRPC streams) drain for up to 20 seconds, then
the remaining tunnels are closed; idle tunnels close immediately. The rules and admin servers shut down
within the same bound and the decision queue is flushed for at most five seconds.
Deployment termination grace must cover those budgets.
There is no transparent TCP/HTTP2 migration, request replay, or promise to finish an unbounded stream.
Staging runs two replicas with `RollingUpdate` (`maxUnavailable: 0`, `maxSurge: 1`) and a
PodDisruptionBudget of one available, so an image roll never takes a ready endpoint away before its
replacement is ready, and a voluntary eviction keeps one.

Both replicas are pinned to the `hil-ovh` zone by node selector with no anti-affinity or topology
spread, so they may share a node; the PDB limits voluntary evictions, not involuntary failures.
Testing runs one replica with `Recreate`. Both use a 60-second
termination grace and gRPC health probes on the admin listener: readiness on the empty service, liveness on `livez`.
