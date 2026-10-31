# Kubernetes API aggregation for Agentplane services: design exploration

Status: **design exploration, held by the operator, 2026-10-17.** It is not a decision record, and
nothing is scheduled. It authorizes no extension apiserver, API group, `APIService` or deployment
change. If the idea is revived, what survives graduates to a doc under `docs/`.

The candidate table below is also a 2026-10-17 snapshot: the Action Service now runs in Go behind
a generated HTTP facade.

An aggregated API is an extension apiserver that the kube-apiserver proxies one API group to,
registered by an `APIService`. Clients get the cluster's authentication, RBAC, discovery, OpenAPI
and list/watch conventions, so `kubectl`, `client-go` and informers work against it. There are two
shapes with different costs:

- **Facade.** The extension server implements the resource's REST handlers over the owning
  service's own store or gRPC API. Nothing moves into etcd.
- **Own storage.** Objects live in etcd, or in a SQL shim such as kine, owned by the extension
  server.

The facade is the shape that fits. The [session control](../docs/adr_sandbox_session_control.md) rule "keep
per-Session data out of Kubernetes" means out of the cluster's control-plane etcd: its Raft, fsync,
memory and SSD cost buys cluster state, not agent traces, retained history or a value rewritten on
every tool call such as last-active time. A facade over a service database stays out of it. A store of the
extension server's own is weighed under Alternatives considered. Sandbox-level facts are already CRDs,
which need no extension server. The question is which service-owned, caller-facing records are worth
presenting through the Kubernetes API.

## What it would buy

- **RBAC as the authorization model, but not only through aggregation.** Every service TokenReviews
  each bearer ([workload authentication](../docs/workload_authentication.md)) and keeps its own allowlist
  (`caller_accounts`, `reader_accounts`, `wake_caller_accounts`). Roles assigned by the existing
  `PolicySet` `kubernetes_role_binding` rules, with `expiresAt`, could replace them, and
  `kubectl auth can-i` would answer for the real policy. A service can ask `SubjectAccessReview`
  itself for the TokenReviewed caller, with resource strings it names, as `cluster/proxies/loki_read_proxy`
  does with `SelfSubjectAccessReview`; workload authentication defers it for the services. Behind an
  `APIService` the kube-apiserver makes that check before the request arrives, and the extension
  server asks it again for the original user.
- **`kubectl` as the agent's tool.** [Principles](../docs/principles.md#agent-facing-affordances-are-scriptable-apis)
  already call `kubectl` the agent's Kubernetes access. A Sandbox's kubeconfig reaches the API server
  through the egress proxy as the Sandbox's own ServiceAccount, so no new reachability or credential.
- **Standard list, watch and wait.** `resourceVersion` watches, `kubectl wait`, continue-token
  paging, OpenAPI and generated clients replace hand-written snapshot loops
  (`action_service/api/watch.go`) and per-service wait parameters.
- **Garbage collection by owner.** A record whose lifetime is a Sandbox's can carry an `ownerReference` to
  its UID and be garbage-collected with it. This does not apply to History, which must outlive the
  Sandbox.

## What it costs

- **A Go apiserver in each hosting service.** `k8s.io/apiserver` is Go. `third_party/go.mod` carries
  it, but the repo uses only its `RequestInfo` parser (`haku/kube_api_proxy`). A resource needs
  hand-written typed objects, generated deepcopy and OpenAPI, serving certificates, an `APIService`
  with its CA bundle, and bindings to `system:auth-delegator` and the
  `extension-apiserver-authentication-reader` Role. The scaffolds (`apiserver-builder-alpha`,
  `apiserver-runtime`) are alpha. The Action Service is Python: it would be Go
  first, or fronted by a Go bridge over its gRPC.
- **Watch over Postgres.** A watch needs a monotonic `resourceVersion`, resume from one, and `410 Gone`
  when it cannot. The watches here (`WatchDispatches`, `WatchSessionSummaries`, `/decisions/changes`,
  the operator SSE streams) send a snapshot then changes, or a payload-free `changed` tick, and
  cannot resume. A correct one needs a transactional per-table sequence and a rule for rows that
  become visible out of commit order.
- **Hot fields stay out of the resource.** A status field that changes at tool-call rate, such as
  last-active time, would bump the object's `resourceVersion` and wake every watcher at that rate. A
  watcher cannot opt out: a field selector selects objects by a field's value, it does not project
  fields, and each event carries the whole object. The write itself would also add a revision each
  time in etcd or kine. Such values stay on the service's own API, or move to a separate object
  that changes at a bounded rate, as kubelet heartbeats moved from Node status to Leases; the
  session-control design already puts idleness on a Lease renewal that happens anyway.
- **One `APIService` per group and version per cluster.** Staging and testing are two namespaces of
  one cluster, and already share their cluster-scoped CRDs (`cluster/cdk8s/agentplane_crds.py`). An
  `APIService` names one backing Service, so testing cannot run a changed extension server beside
  staging's under the same group. A group per environment makes the group, Roles and every client
  environment-specific.
- **Cluster-wide failure coupling.** While an `APIService` is unavailable, discovery of its group
  fails for every client and namespace deletion stalls on `NamespaceDeletionDiscoveryFailure`;
  discovery has a five-second budget. Services already depend on the kube-apiserver for TokenReview
  on every call, so the dependency is not new, but its blast radius is.
- **The control plane in the data path.** Large or hot reads must not proxy through the
  kube-apiserver: a retained Codex event was observed at about 14 MiB
  (`agentplane/grpc/options.py`), and the egress decision log records each CONNECT and HTTP request.
  Whether the aggregator relays gRPC is not established; the streaming precedent in this repo is
  KubeVirt's console over WebSocket (`agentplane/app/go/sandboxes/console.go`).
- **Authorization RBAC cannot express.** `resourceNames` match by name, not UID, so a grant outlives
  a same-name replacement (the Policy Controller deletes native bindings for the same reason).
  Collection membership, per-caller argument redaction and "own requests only" are code in the
  extension server's storage layer, so RBAC is a coarse verb gate over them, not the whole policy.
  External MCP callers (OAuth bound to a ServiceAccount) and operators (Authentik OIDC at the app)
  authenticate to the services, not to the kube-apiserver, so their routes stay.
- **A further agent-facing surface.** The approved direction is gRPC, a gRPC-Gateway HTTP/JSON facade
  and a CLI, with MCP besides ([API transports](../docs/runner_delivery_and_harness_design.md#api-transports-and-agent-workflows)).
  An aggregated API is another route to the same record. Under
  [explain overlapping paths](../docs/principles.md#explain-overlapping-paths) it earns its place by
  replacing a surface for that service, not by being added.

## Candidates, checked against the code

| Resource                                                                                                | Fit                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Verdict                                           |
| ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `ActionRequest`, workload half (`/v1/action-requests`, `agentplane/action_service/api`)                 | Caller-owned, low volume, spec (action, arguments, title) and status (state, Decisions, execution). The wait on the receipt and the paging of the list, which the [agent CLI](../docs/agent_cli.md) must add to the service, are `kubectl wait` and `limit`/`continue`. The idempotency key is any string of up to 200 characters and a repeat is refused, while names are DNS subdomains: it stays a field with a uniqueness index, recovered with a field selector.                                                                                                                                                                                                                                                                                                                                                                                                                                                     | **First candidate.**                              |
| `Subscription`, with an `inboxes/acknowledgement` subresource (`notification_service/api/`)             | `version` is a `resourceVersion`, the immutable `creation` is spec, worker-written `error` and `last_success_at` are status. Cancel cannot be `DELETE`: a cancelled key is retained so a retry returns it. Inbox entries are a cursor-paged log of provider payloads and stay API calls. The service is being reconsidered, so group and kind names would freeze its current shape.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | Not yet.                                          |
| `Dispatch`, a `sandboxes/dispatches` subresource (`runner_gateway/dispatcher/session_dispatcher.proto`) | The best structural fit, and already Go. `command_id` is the name and `sandbox_uid` a spec field; delivery state and `reason` are status. Roles could replace `caller_accounts` and `wake_caller_accounts`, with "send to any Sandbox in a namespace" as one grant, but a `SubjectAccessReview` from the Dispatcher gets that without an extension server, and without the delegated caller proof a facade needs for per-caller wake charges. What aggregation adds is `kubectl`, informers and `kubectl wait` for a record whose callers are services. Gaps: a repeated `command_id` returns the stored record where `create` answers `AlreadyExists`. `WatchDispatches` re-lists on each change, so a `resourceVersion` needs a change sequence in the queue schema. `SubmitInput` is the operator's authority and agents would not get it by default. A record per message must stay out of etcd, which a facade does. | No. Take its Roles through `SubjectAccessReview`. |
| History `Session` summary and events (`history_service/history_service.proto`)                          | `SessionSummary` fits get, list and watch. Events (cursor paging, megabytes each) and Thread entities do not, and are most of the traffic. Authorization is by recorded Sandbox UID and must hold after the Sandbox is deleted: RBAC matches names, and collection membership is not an RBAC concept.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | No. It neither helps nor blocks that policy.      |
| Egress decisions, `/v1/rules`, `/v1/action-policy`                                                      | Decisions are a best-effort log of each admitted CONNECT and HTTP request (`egress/SPEC.md`), the wrong shape for a watched resource. Rules and policy are computed "what can I do" views over CRDs: a create-only review resource like `SelfSubjectRulesReview`, with no storage or watch.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | No. The views are the cheapest spike vehicle.     |
| Sandbox subresources (console, exec, Runner proxy)                                                      | The console is one already: the app attaches `virtualmachineinstances/console` through KubeVirt's `subresources.kubevirt.io` aggregated API, granted by RBAC (`cluster/cdk8s/agentplane/app.py`) and implemented in `agentplane/app/go/sandboxes/console.go`. A VM guest has no exec path by design ([VM Sandboxes](../docs/vm_sandboxes.md)); a Pod box's exec is the native `pods/exec`; the Runner proxy relays every method of the `Runner` gRPC service.                                                                                                                                                                                                                                                                                                                                                                                                                                                             | No.                                               |

## Recommendation

Authorization and aggregation are separable, and the cheaper half comes first. A service can ask
`SubjectAccessReview` whether the TokenReviewed caller may act, with Roles assigned by `PolicySet`,
and no extension server; that route replaces the allowlists on its own and belongs to the sender-grant
and agent-authority choices. Aggregation then adds one thing: a Kubernetes client surface
(`kubectl get` and `wait`, informers, OpenAPI) for a record. It is worth its cost only where an agent
or operator would use that surface. For every other record the cost exceeds the gain today.

**First candidate: `ActionRequest`'s workload half, once the Action Service is served from Go.** It is
the one record that is caller-owned, low-volume, spec-and-status-shaped and agent-facing, and it
removes the wait, paging and key-recovery work that the CLI would otherwise carry. The store behind the
facade is open then: its own tables, kine, or a dedicated etcd. A Python bridge adds a service to
delete, so none is built.

A bounded spike answers the questions that would end the idea. Its answers matter only once
`ActionRequest` is committed, so it can wait for that. The Go Dispatcher, already deployed on the k3s
environment (`agentplane/system_tests/k3s`), is the cheapest vehicle: a read-only `Dispatch` list, get and
watch, served by an extension server in its own Deployment, so that the server's outage is not an
`APIService` outage, which breaks discovery cluster-wide. It asks:

1. Whether a group per environment on the shared cluster is workable for Roles and clients.
2. What an unavailable extension server does to discovery and namespace deletion on this cluster.
3. Whether the Pod name and UID of a bound token reach the extension server as user `extra` after the
   front-proxy hop; the workload principal depends on both.
4. Whether `kubectl wait --for=condition=...` over `Decided` and `Terminal` conditions covers what the
   CLI's outcome exit codes do.

Keeping the option open costs one column: when the Action Service's schema is rewritten, give request
rows a per-table transactional version, so a `resourceVersion` watch can be layered later.

A second agent-facing service adopting the same server would amortize its cost. An operator OIDC
issuer trusted by the kube-apiserver would let the operator routes follow; neither is assumed.

## Alternatives considered

- **CRDs for Session-level records.** The same API shape without an extension server, but etcd stores
  them: per-Session data in etcd at etcd's write rate, which the session-control ADR rejected for the
  command queue. CRDs remain right for Sandbox-level facts, and are used for them.
- **A dedicated etcd for the extension server.** It isolates the control plane, but upstream etcd
  limits a request to 1.5 MiB and a backend to 2 GiB by default (neither checked against this
  cluster's etcd), against a retained event observed at about 14 MiB, so bulk Session data cannot
  live there. It adds a stateful system beside CNPG's backup and restore, and `Dispatch` records
  accumulate with every message for the Sandbox's lifetime. A low-volume record such as
  `ActionRequest` would fit.
- **kine, or another etcd-compatible shim over Postgres.** It supplies `resourceVersion` and watch
  over SQL, but stores objects as opaque rows in its own table. A service with queue logic in SQL
  (the Dispatcher's per-Session FIFO, Lease fences and wake budget) would move that into kine and
  lose its constraints and queries, or keep two copies. It appends a revision per write until
  compaction (upstream design, untested here), which a hot field would multiply. Nothing in this repo
  uses it yet.
