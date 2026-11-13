# Agentplane egress proxy

The central proxy of Agentplane's credentialless egress: a Go forward proxy that proves each
caller's workload SVID over mTLS, decides the request from the `egress` rules of `PolicySet`s a
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
  One forward listener on port 8888 authenticates workload SVIDs.
- `identity`: resolves each request's SVID to its live `WorkloadCredential`; its required Identity
  subject is the policy subject.
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

- `sidecar/`: the Go runtime behind image `agentplane-egress-sidecar`. It presents the
  sidecar-held SVID in the TLS handshake to the proxy, whose certificate trust and fixed SPIFFE
  identity, `spiffe://agentplane/egress-proxy`, it verifies; `proxy.transport` keeps its tagged
  `mode: tls` and `auth.mode: spiffe`, their only values. The sidecar sends no workload bearer to
  the central proxy.
  It also hosts local generic gRPC routes to Agentplane services, each with an explicit transport:
  `plaintext`, or `tls_connect` with the sidecar SVID. The runtime uses no Python interpreter. Its
  settings come only from the YAML file named by `AGENTPLANE_EGRESS_SIDECAR_CONFIG_FILE`, which it
  refuses to start without; it reads no per-setting env and accepts no flags.
- Workload HTTP routes carry only the upstream authority and exact service URI. Their destination
  TLS uses the same Sandbox SVID and CA configured on `proxy.transport`; the central proxy sees an
  authenticated CONNECT and does not terminate the service TLS connection or assert a caller.
- `sidecar_config.py` and `platform_routes.py`: Python settings and route models cdk8s renders each
  Sandbox template's shared listener and per-service route settings from. The process entry point and both forwarding
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
The `forward` listener requires serving TLS files and an `auth` list containing SPIFFE with its
trust bundle. The proxy presents `spiffe://agentplane/egress-proxy` and requires a Runtime SVID
with explicit clientAuth. Every request checks the live Sandbox UID, accepted credential and Pod
binding before reading policy. The subject is the record's Identity in `rules_namespace`, fenced on
its UID. Clearing
or replacing acceptance, changing the binding, or removing the Sandbox revokes access; quiescence
keeps the accepted credential available for final History sealing. Caller headers cannot select a
subject, and bearer credentials cannot authenticate the hop.

The retired `X-Agentplane-Workload-Token` header is discarded by both forwarding hops and never
participates in authentication or credential substitution. History, Notification, Dispatcher,
Action, LLM and Rules verify workload SVIDs themselves; central carries their TLS opaquely and
never asserts the caller.

The real-binary `test_proxy.py` harness checks accepted SVIDs, revocation, forged subjects, expired
and forged leaves, untrusted workload-token header stripping, and refusal of bearer hop authentication.

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

## Kubernetes accounts

A `kubernetesServiceAccount` credential acts as a ServiceAccount on the proxy's own API server by
impersonation (contract: <SPEC.md> § Decision). The deployment sets `local_api_server` (host, port,
and the proxy's automounted token file) and renders the accounts namespace with a Role granting the
proxy `impersonate` on `serviceaccounts` there and nowhere else
(<../../cluster/cdk8s/agentplane/kubernetes_accounts.py>). Impersonating ServiceAccounts rather
than users keeps OIDC users bound by name out of reach. A TokenRequest source (minted, cached
account tokens) is not built: it is needed only for a remote API server that refuses the proxy
impersonation rights.

## Rules API

`agentplane egress rules` calls native `EgressRules.GetRules` through the sidecar's shared
platform gRPC listener on port 3130. That local listener dispatches by exact service prefix. The
sidecar holds the credential and presents the runner SVID to the destination.
SVID calls establish verified TLS through an opaque central CONNECT to the shared gRPC port 8081;
the server verifies its actual peer using the forward hop's authentication policy. No identity is
asserted by the proxy. Rules has no separate audience, certificate or HTTP API. Health probes and
operator RPCs keep their independent authorization on that shared port.

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

The shared gRPC listener also serves Rules under the forward hop's shared authentication policy.

The shared port serves native gRPC over TLS; Rules accepts the same SVID authentication as the forward hop, while probes and
operator bearer methods use the same verified TLS listener with their own method policy. On shutdown it ends the open watches, then stops gracefully: the
other calls in flight finish within the drain deadline.

Every `EgressAdmin` call admits only the ServiceAccounts in `admin_grpc_callers`, each proving itself by
a bearer of audience `admin_grpc_audience` that is reviewed for every call (an acceptance is reused
for 30 s), before anything is read
(<../docs/grpc_services.md>). Watches periodically revalidate authorization and end on revocation or when the proxy drains;
reconnecting callers are admitted afresh.

The health service is always mounted. `EgressAdmin` is mounted only when `admin_grpc_audience` and
`admin_grpc_callers` (`namespace/name` entries) are both set. Health is always served; Rules admission follows the workload transport policy above.

## ServiceAccount permissions

In the rules namespace (`--rules-namespace`): `get`, `list`, `watch` on `egresscredentials`, `policysets` and `policybindings`, and `patch` on
`policybindings/status`. Each admitted Sandbox namespace grants only `get`, `list` and `watch` on Sandboxes. The proxy
uses the Sandbox's own ServiceAccount name without reading ServiceAccount or Pod objects. In the credentials namespace
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
borrowing becomes a real concern — for example, where workloads can obtain another subject's SVID or
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
TODO: a row names an Identity subject by name alone, as it does a ServiceAccount, so a recreated
Identity of the same name shares its predecessor's history; a UID column is the additive fix.
Staging and testing each hold an `egress` database in their CNPG cluster. Both use Goose; existing
history rows and the inert `egress_alembic_version` table remain in place. The proxy migrates before starting its
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
termination grace and verified TLS exec probes on the admin listener: readiness on the empty service, liveness on `livez`.
