# Workload identity lifecycle

Status: **inventory open, 2026-11-10; the proxy's bearer hop is removed.** Decision node
[`APP_AGENT_TOKEN_DECISION`](task_dag.md#app_agent_token_decision--the-apps-agentplane-agent-token-under-the-target).
The issuer, the `WorkloadCredential` kind, the resolver and every workload requester have landed:
Sandboxes, Action command/build boxes, and the standing records for Haku (`haku-ui`, `haku-anki`,
`haku-jupyter`) and the `haku-ci` relay. Their contract is in
[workload authentication](../docs/workload_authentication.md#workloadcredentials). This plan
inventories the paths that do not yet resolve to an `Identity` through one of the
[three credential forms](identity_and_kubernetes_accounts.md#target-one-identity-three-credential-forms).

## Remaining paths

Audited against the code on 2026-11-10. Buckets: **(a)** Kubernetes-API use, kept; **(b)** an
Agentplane API that moves to SVID mTLS; **(c)** a caller that may need a non-SVID form; **(d)** dead;
**(o)** MCP edge OAuth, a target form whose binding still ends at a ServiceAccount. **(s)** Agentplane
services calling each other as themselves, kept on TokenReview (operator, 2026-11-10). Ordered by what
to do first.

| #   | Path                                            | Evidence                                                                                                                                                                                | Accepts today                                                                                                                     | Callers on the token path                                                                                                 | Bucket  | Next step / blocker                                                                                                                          |
| --- | ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | MCP edge to the Action Service                  | `mcp_edge/core_client.go:96`, `mcp_edge/core_client.go:138`, `action_service/api/gate.go:153`                                                                                           | The edge mints a TokenRequest for the Connection's ServiceAccount and forwards it as `Bearer`; the Action Service TokenReviews it | Every claude.ai Connection, acting as `claude-ai`                                                                         | (o)     | The edge authenticates with its own SVID and delegates the Identity (identity plan § MCP edge), at `IDENTITY_CUTOVER`                        |
| 2   | MCP edge Connection binding                     | `mcp_edge/authserver/tokens.go:296`, `mcp_edge/connections/service.go:100`, `mcp_edge/connections/authorizations.go:133`, `mcp_edge/config.go:40`                                       | An access token resolves to its Connection's `bound_caller`, a ServiceAccount; `connection_identities` is parsed but not rendered | The same Connections                                                                                                      | (o)     | `bound_caller` and `original_caller` become Identities; `connection_identities` set, at `IDENTITY_CUTOVER`                                   |
| 3   | Projected Kubernetes token beside the SVID      | `cluster/cdk8s/agentplane/sandbox_pod.py:44`, `launcher_relay.py` (Kyverno), `action_service/sandbox/inventory.go:356`, `egress/identity/identity.go:116`, `egress.py:272`              | The SVID authenticates; a second TokenReview binds the forwarded token to the record's Pod for `kubectl` substitution             | Every staging Sandbox Pod and virt-launcher (`identity_catalog` is off), every command/build box                          | (b)→(a) | `kubectl` moves to impersonation: `EGRESS_IDENTITY_RULES`, then `SANDBOX_IDENTITY_SPEC` (drafts #15239, #15258); boxes at `IDENTITY_CUTOVER` |
| 4   | SVIDs and bindings name ServiceAccounts         | `workload_auth/auth/svid.go:72`, `egress/identity/svid.go:31`, `sandbox_controller/identity.go:27`, `cluster/cdk8s/agentplane/standing_caller.py:30`, `action_service/api/policy.go:43` | Every record and staging binding names a ServiceAccount subject; an SVID with an Identity subject is refused                      | All SVID callers; the Sandbox Controller creates one account per Sandbox; policy views key on `ServiceAccountRef`         | (b)     | `SERVICES_IDENTITY_PRINCIPAL`, `EGRESS_IDENTITY_RULES`; requesters and bindings switch at `IDENTITY_CUTOVER`                                 |
| 5   | App bearer for `agentplane-agent`               | `app/go/auth/auth.go:506`, `cluster/cdk8s/agentplane/app_settings.py:212`                                                                                                               | Operator OIDC session, or a TokenReview naming `agentplane-agent`                                                                 | System tests (`system_tests/driver/cluster.py:74`, `deployment/stack.py:570`) and hand-run scripts (`app/README.md`)      | (c)     | Open question 1                                                                                                                              |
| 6   | Backend callers of the shared service listeners | `history_service.py:194`, `notifications.py:202`, `runner_gateway_workload.py:266`, `llm_ingress.py:212`, `actions.py:382`                                                              | SVID, or each service's audience by TokenReview                                                                                   | The app, Notification, Action Service, Sandbox Controller, Runner Gateway and central sampler, each as itself             | (s)     | Kept: service-to-service TokenReview (operator, 2026-11-10)                                                                                  |
| 7   | Bearer-only service listeners                   | `egress/cmd/admin_grpc.go:103`, `policy_controller/cmd/main.go:146`, `central_sampler/cmd/main.go:69`, `runner_gateway/main.go:267`                                                     | TokenReview alone; the Policy Service listens without TLS                                                                         | The app (egress admin, Policy Service, Gateway), Notification and the Sandbox Controller (Gateway), the Gateway (sampler) | (s)     | Kept: service-to-service TokenReview (operator, 2026-11-10)                                                                                  |
| 8   | Delegated calls                                 | `workload_auth/auth/delegation.go:57`, `runner_gateway/main.go:347`                                                                                                                     | A Pod-bound TokenReview proves the actor; the envelope names a ServiceAccount effective caller                                    | The Action Service toward Dispatcher and History                                                                          | (s)     | Actor stays TokenReview; the effective caller becomes an Identity at `SERVICES_IDENTITY_PRINCIPAL`                                           |

The claude.ai policy view's `subject: {namespace: agentplane-staging, name: claude-ai}` is not a
rendering artifact: staging's `claude-ai` `PolicyBinding` names the ServiceAccount, since
`StandingCaller.subject` renders an Identity only with `identity_catalog`, and `GetActionPolicy`
resolves bindings by `ServiceAccountRef`. The OAuth credential itself (edge-issued access tokens for
dynamically registered clients) is one of the target forms; rows 1 and 2 are what still ties it to
an account.

Kept, bucket (a): every controller and service reaching the Kubernetes API as its own account, the
egress proxy's own token for impersonation (`local_api_server`), Flux and its impersonated appliers.
The edge's TokenRequest right on `claude-ai` is Kubernetes-API use too, but exists only for row 1
and goes with it.

The central sampler holds no SVID, so rows 6 to 8 include it. Nothing acts as a central Session's
identity yet (`CENTRAL_SESSION_ACTING_DESIGN`); its own calls are a backend
service's.

**Per-incarnation credential IDs: revisit after the plan lands (operator, 2026-11-09).** The
cluster has no CRL or OCSP, so a fresh ID per accepted binding is what makes replacing an
incarnation revoke the old key, even one left in a terminating Pod, a VM disk or a Firecracker
snapshot. The alternative is one stable ID per record plus the Pod-binding check, which covers only
callers that forward a Pod-bound token. Revisit if the ID check proves redundant with binding checks
on every route, or if reissue latency hurts startup. Nothing in the migration waits on it.

## Authorization

**Coarse (operator, 2026-11-09).** RBAC grants `create`, `update` and `delete` on
`workloadcredentials` to exactly three accounts: the Sandbox Controller, the Action Service and
Flux's applier. Each may name any subject: a ServiceAccount in the record's namespace until the
cutover, and after it any `Identity` in the policy namespace. So each of those accounts can make a
workload act as any identity in its namespace. The real gates stay upstream:

- for a Sandbox, who may set `Sandbox.spec.identity`;
- for a box, the caller the Action Service authenticated;
- for a standing workload, git.

Flux's kustomize-controller already holds `*`, so a narrower grant to Flux would record intent but
not enforce it. Haku cannot author a record, because the `haku-state` Kustomization impersonates
`haku-state-reconciler`, which holds no right on `WorkloadCredential`s. A narrower requester can be
added later without changing anything here: a `ValidatingAdmissionPolicy` calling the CEL
`authorizer` for a custom `use` verb on the named identity.

## After the bearer hop

The proxy's `forward` listener authenticates SVIDs alone. Its `allowed_service_account_namespaces`
now serves only projected-token reviews (row 3), which `public-coder-agent` and the standing
namespaces never send, and the Action Service's `caller_service_account_namespaces` keeps
`public-coder-agent` for no caller. `kubernetes_bearer` stays on service listeners for rows 1, 6
and 7 until they move.

## With `IDENTITY_CUTOVER`

The ordering edge and the owner switch were decided by the operator on 2026-11-09. Box creation
needs the caller's own ServiceAccount: `stamp` in `action_service/sandbox/inventory.go` reads it, runs
the box as it, names it as the record's subject, and makes its controller, the calling Sandbox, the
record's owner, which owns the box. `SANDBOX_IDENTITY_SPEC` stops creating that account. A Sandbox
caller's boxes already hang off the Sandbox, not the account; with `SERVICES_IDENTITY_PRINCIPAL`,
which gives the Action Service its caller as an Identity, a box names that Identity as its subject
and runs on the default ServiceAccount, and a caller with no Sandbox has its records owned by its
`Identity`. Its `kubectl` moves to impersonation with `EGRESS_IDENTITY_RULES`. Until the cutover,
requests name ServiceAccounts and services see unchanged subjects. The cutover PR switches every
requester's `subject` to an identity, and cdk8s renders `haku-ui` and the `haku-ci` relay's account
as `Identity`s. The resolver already reads `spec.subject.identity` live with its UID, in the policy
namespace rather than the record's own, which matters for `haku-sandbox` and `haku-ci` records.

## Open questions

1. **The app's `agentplane-agent` bearer (row 5).** Its callers are system tests and hand-run
   scripts reaching the app through its public route, where TLS ends at the Gateway, so a client
   certificate cannot reach the app as mTLS there; that is the one path that seems to need a header
   form. Options: the tests call the backend services in-cluster with a standing SVID instead of the
   app's API (the app is a client of those services,
   [dependency rule](../docs/service_boundaries.md)); the tests reach the app's in-cluster listener
   with a client SVID; or the route stays a TokenReview exception, contrary to the target. Question
   for the operator: which.
2. **Off-cluster workloads presenting SVIDs.** A Secret mount does not reach them. Recommendation:
   design delivery when one exists. The record and the resolver stay as they are. External MCP
   clients use the edge's OAuth form instead.

When rows 1 to 4 are gone and the open questions are settled, the rest of this content moves into
`docs/workload_authentication.md` and the SPECs of the Policy Controller, Sandbox Controller, egress
proxy, Action Service and MCP edge, and this file is deleted.
