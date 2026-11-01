# Generated HTTP facades: what is left

The facade contract and the browser path through the app's upstream proxy are in
[gRPC services](../docs/grpc_services.md#httpjson-facades), the approved direction and rejected
alternatives in [API transports](../docs/runner_delivery_and_harness_design.md#api-transports-and-agent-workflows).
The per-service tracks are in the [task DAG](task_dag.md#service-api-transport-tracks). This plan
holds what is still open.

The four Thread command writes (`SubmitInput`, `ChangeModel`, `ChangeReasoningEffort`, and
`InterruptTurn`) use generated Dispatcher routes through the operator-session proxy. The duplicate
Thread handlers and their History/model-catalog preflights were removed in
[#13469](https://github.com/agentydragon/ducktape/pull/13469). Retained commands pin their Sandbox
name and UID; uncertain outcomes reconcile against Dispatcher and History. The app serves the human
frontend; agent and service callers use native gRPC at the owning backend.

## Open

- **History reads.** History has no facade; the app still mirrors `ReadEntityEvidenceResponse`,
  `ReadNativeFramesResponse` and the observation pages in Go structs (`app/go/historydata/historydata.go`). `HISTORY_READS_FACADE` replaces them. A browser watch could use the implemented
  [`FollowSession`](../history_service/README.md#read-and-follow-one-session); the browser currently stays on Electric.
- **Thread checks on History reads.** `thread_observations` answers 404 for a Session no Thread links;
  a proxied read answers for any Session the History Service holds. All operators are equal here, so
  this is a behavior to keep or drop on purpose, not a hole.
- **Remaining app contracts.** `APP_API_GRPC_FACADE` in the
  [task DAG](task_dag.md#app_api_grpc_facade--uniform-app-grpc-apis-with-generated-http-facades)
  covers the remaining app views, mutations and streams. Session opening still applies browser
  presets and creates Thread metadata before calling Dispatcher; separate that human-facing
  orchestration from native workload admission and reconciliation. Decide which checks each
  remaining wrapper needs, at its owning boundary, rather than preserve them as transport requirements.
- **Error codes in the browser.** `sandbox_page.tsx` and `connections/management.tsx` (three times)
  branch on HTTP status; they move to the body's gRPC `code`, which `HttpError` then carries.
- **Attribution.** Services see the app, not the operator; the Dispatcher's per-caller wake budget is
  per app. Per-operator attribution is the separate delegated-caller question.
- **Git index.** Its `/search` gets a facade with its Go port, if an agent needs it.

## Direct browser access, later

_Through the app_ keeps one browser origin, one login and one CSRF check, and leaves the History
Service, Dispatcher, Notification Service and Egress admin API admitting only service accounts. The
operator would accept _direct_ calls from the browser to each facade if they work well, and chose the
app first as the lower risk (2026-10-19). Protos, annotations and facades are identical either way;
only the proxy hop and the credential change. A service takes it only once these hold:

- **A bearer the browser can hold.** The browser cannot run the Authentik exchange; the login access
  token it is made from stays in `operator_browser_session`. The route
  [operator federation](../docs/operator_federation.md) records as available is one
  cookie-authenticated app call that returns a short-lived token from the same Authentik exchange the
  Action Service already validates, which the browser sends in a header. Never an app-signed token.
  The cost is the one federation names: the token is readable by script for its lifetime, where the
  `__Host-` cookie never is, and revocation is lifetime-bounded rather than immediate.
- **Operator admission in each service.** Each service validates issuer, audience and signature
  beside its ServiceAccount TokenReview, as `action_service/api/operator_oidc.go` does. Each target
  needs its own Authentik provider, application and policy under `tf/gitops/sso-providers`, or all
  share one operator audience, which makes one bearer acceptable everywhere. Choose between them when
  the first service goes direct.
- **Edge routing.** One hostname with a path per facade avoids CORS. The Gateway API already routes
  the app and the public MCP/OAuth routes this way. That public origin keeps operator endpoints off
  it on purpose, so an operator facade needs a separate route.
- **Streams end at token expiry.** The browser reopens with a fresh token on `reconnect_required`,
  the frame `WatchSessionSummaries` already carries. Logout no longer ends a stream; expiry does.
- **What the app supplies today moves or is dropped on purpose:** the Thread checks on History reads,
  and the Dispatcher's wake budget, keyed on a service account now, which would need the operator's
  identity to attribute a wake to them. CSRF stops applying, because the credential is not ambient.

Revisit when the proxy hop costs something measurable (a stream held open per tab, per-upstream
settings), or when per-operator attribution is wanted.
