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
  the request's placeholder directs it to, substitution, binding resolution. No I/O.
- `identity.py`: the shared `sandbox_auth` TokenReview/live-owner resolver plus the egress-only
  source-Pod address check and expiry-bounded verdict cache.
- `upstream.py`: the admitted host resolved by the proxy, refused when it points anywhere not
  globally reachable, and pinned so the dial goes to the address checked.
- `informer.py`: read-only list-and-watch of the five kinds into each replica’s `Index`.
- `rules_api.py`: the agent-facing
  `agentplane-egress.agentplane-staging.svc.cluster.local/v1/rules` API and the narrow
  independently authenticated FastAPI listener and shared `RulesProjection`; `addon.py` is the
  ordinary mitmproxy policy/substitution gate. `decisions.py` defines admission records;
  `decision_log.py` queues them for `decision_store.py`. `admin.py` serves history, local
  binding observations, readiness and liveness.
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
runner-side credential reference or Agentplane owns an equivalent broker at that boundary; the
boundary and the candidate rewrite are in <../docs/buildbuddy_remote_auth.md>.

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
published in nonsecret runner instructions. The default `basic` policy binds this exact
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
The Service may select a different replica from the one that admitted the request. The answer is
an informational snapshot of the answering replica, not a global acknowledgement or admission promise.

## ServiceAccount permissions

In the sandbox namespace: `get`, `list`, `watch` on `egresspolicies`, `egressbindings`,
`egresscredentials`, `sandboxes.agents.x-k8s.io` and `pods`. In the
credentials namespace (`--credentials-namespace`, `agentplane-egress-credentials` by default):
`get`, `list`, `watch` on `secrets`, and nothing in the sandbox namespace. Cluster-wide: `create`
on `tokenreviews.authentication.k8s.io`. There are no Kubernetes status writes or leader election.

Substituted credentials live in a namespace of their own because RBAC cannot filter Secrets by
label: a namespace-wide read in the sandbox namespace would hand the proxy the model key and the
database credential along with the ones it is meant to substitute.

## Decision history

`DecisionLog.record` puts an immutable `DecisionRecord` on a bounded `asyncio.Queue` (2,000 by
default, `--decision-queue-size`) and returns; admission never awaits database IO. One writer task
drains up to 100 records per batch (`--decision-batch-size`), waking at least once a minute when
idle, and writes through an async SQLAlchemy/asyncpg engine with a pool of two connections, no
overflow, and two-second pool, connect and command timeouts. A batch gets three attempts with
exponential backoff (1–4 s); a retry reuses the batch's event IDs and decision timestamps, and the
insert is `ON CONFLICT (event_id) DO NOTHING`. Records already past the retention window are dropped
before the write and counted as expired. Shutdown flushes for `--decision-flush-seconds` (5 s by
default) and counts what remains as shutdown loss.

After every batch or idle wake the writer deletes at most 1,000 expired rows, selected `FOR UPDATE
SKIP LOCKED` on the `(decided_at, event_id)` index so concurrent replicas neither block nor
contend. Indexed deletion is used rather than time partitions: no measured volume justifies
partition management, and event-ID idempotence stays global. Monitor database size and
`cleanup_failures`; sustained ingestion beyond cleanup capacity is the signal to revisit that.

The API's `at` field is the `decided_at` column; `ingested_at` is the row's insertion timestamp.
`DecisionStore.recent` takes the newest `--decision-history-size` rows (200 by default, at most
1,000) inside the retention window and returns them oldest first.

The schema lives in `migrations/` with its own Alembic version table (`egress_alembic_version`);
`database_migrate.py` serialises concurrent runs with a transaction-scoped advisory lock. Staging
and testing each hold an `egress` database in their CNPG cluster and run the separately published
`agentplane-egress-migrate` image as the proxy Pod's init container, so a failed migration keeps
that Pod unready while the previous proxies keep enforcing.

## Open questions

- **Whether an agent also reads its own recent decisions.** Shared history answers "why was I
  denied", and a failure the agent can diagnose itself is the practical win; nothing serves it to
  the agent-facing surface today.
- **Whether that surface versions separately from the operator API.** Agents are long-lived and
  roll independently of the app, so the two may not be able to move together for long.

## Replica lifecycle

`/healthz` and every CONNECT/HTTP admission share `Index.available`: all five kinds must be
initially synced and have cycle timestamps no older than three configured resync periods
(default 900 seconds). Negative clock ages also fail closed. DB health does not participate.
`/livez` only proves the admin event loop answers; a recoverable watch outage does not cause a
restart loop. Every request on an existing TLS/HTTP2 connection is gated again. After DNS I/O,
a changed policy decision or Sandbox snapshot denies that admission without forwarding or replay.
Revocation is eventually observed independently by each watch, not globally linearizable.

SIGTERM/SIGINT closes admission immediately and uses mitmproxy `Proxyserver.servers.update([])`
to stop listeners without cancelling existing TCP handlers. Admitted HTTP responses drain until
response/error, WebSockets until close, bounded by 20 seconds; then the master shuts down.
The process closes the rules/admin APIs with five-second server grace periods and flushes the
lossy decision queue for at most its configured budget (five seconds by default). Deployment
termination grace must cover those budgets plus master shutdown (20 seconds).
There is no transparent TCP/HTTP2 migration, request replay, or promise to finish an unbounded stream.

The admin-only `/bindings` endpoint reports `scope: replica-local`, observation time, readiness,
and derived binding name/UID/generation, resolution reason, and present/missing policy names.
It neither persists conditions nor claims other replicas observed that generation. The app shows
Kubernetes desired bindings as “configured”, not an enforcement acknowledgement. Expiry is
computed on every observation, with no timer-owned status or transition timestamps.

Staging declares two replicas, RollingUpdate `maxUnavailable: 1`/`maxSurge: 1`, a
`minAvailable: 1` PDB, and hostname topology spread within the existing OVH node selector.
The PDB limits voluntary evictions, not involuntary failures; topology spread is limited to
eligible scheduling domains. Testing retains one replica and `Recreate`. Both use a 60-second
termination grace, readiness on `/healthz` and event-loop liveness on `/livez`.

CLEANUP(added 2026-09-11): Remove the CRD's legacy status schema/printer column and proxy
status-patch RBAC only after every running proxy uses an image containing the read-only informer.
An old informer treats a rejected status patch as a fatal task-group error. Scaling remains
gated on the new proxy image, not just the presence of shared diagnostic history.
