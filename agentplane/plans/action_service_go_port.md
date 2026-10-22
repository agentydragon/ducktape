# Action Service Go port

Status: **in flight (resumed by the operator 2026-10-21).** `ACTION_SERVICE_GO_PORT` in the [task DAG](task_dag.md)
records it, and the operator said to port it (2026-10-19 PDT) and approved the destination below (2026-10-20 PDT).
The plan burns down: a stage leaves when its work lands, and the file goes with the Python retirement.

The port moves the service into one Go binary shaped by its destination, on the same PostgreSQL database and schema
(Alembic head `0028_drop_external_grant`):

- **`actions`**, the core, admits a caller by one path, a workload token of a labeled ServiceAccount, and knows
  nothing of Connections: no grant check at submit or claim, and no grant provenance on requests. Revoking a
  Connection stops only its further requests.
- **Connections, OAuth and `/mcp`** are packages that own their tables and reach the core only through the
  `ActionService` interface. After the flip they become **`mcp-edge`**, a separate binary that calls `actions` over
  gRPC with ServiceAccount tokens it mints for the bound accounts ([below](#after-the-flip-the-edge-split)).
- **Web Push and the executors** stay in `actions` as packages behind one interface each: `push` reads pending
  approvals from the lifecycle, and the executors use the lease protocol.

Out of scope: dropping DCR, a watch resume cursor, `SERVICE_SETTINGS_RETHINK`, renaming a table, channel or setting
key, `WEB_PUSH_SERVICE`, and any schema change but the authorization server's tables and the grant columns Go stops
writing ([below](#stored-data-the-port-must-read-and-write-exactly)).

## What moves

| Python today                                                               |  Lines | Becomes                                                                         |
| -------------------------------------------------------------------------- | -----: | ------------------------------------------------------------------------------- |
| `store.py`, `service.py` (lifecycle, leases, dispatch, sweeps, heartbeats) |   1313 | `store` and `worker` packages, hand-written SQL on `lib/pq`                     |
| `models.py`                                                                |    396 | proto messages                                                                  |
| `api.py`, `operator_stream.py`, `updates.py`, `waits.py`                   |    917 | four proto services with generated facades; the receipt stream replaces waits   |
| `caller_auth.py`, `auth.py`, `operator_oidc.py`                            |    185 | workload `Resolver` plus `PolicyIndex`; operator JWT on `coreos/go-oidc`        |
| `policies/`, `github_policy/`, `policy_view.py`, `policy_informer.py`      |   1205 | decision providers on `policy/api/v1alpha1` types and the egress informer       |
| `connections.py`, `enrollments.py`                                         |    698 | grant and consent authority, same rules, as an edge package                     |
| `oauth.py`, `oauth_settings.py`                                            |    501 | authorization server on `zitadel/oidc` `op`, as an edge package                 |
| `mcp_frontend.py`, `direct_tools.py`, `tool_results.py`                    |    795 | `/mcp` on `modelcontextprotocol/go-sdk`, as an edge package                     |
| `mcp_executor.py`, `mcp_linkage.py`                                        |   1446 | supervisor on the go-sdk client; linkage on `oauthex` and `golang.org/x/oauth2` |
| `sandbox/`                                                                 |    988 | inventory and template logic; exec on client-go `remotecommand`                 |
| `push.py`, `push_settings.py`                                              |    318 | `push` package on `SherClockHolmes/webpush-go`; sends outside any transaction   |
| `settings.py`, `main.py`, `runtime.py`                                     |    344 | `config.go` (Viper, file plus environment, no flags, same keys), `main.go`      |
| Python tests (including `sandbox/` and `policies/` tests)                  | ~11600 | Go tests on real PostgreSQL in the stage that ports their subject               |

## What the Go service uses

| Need                                                        | Go side                                                                                                         |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| gRPC with HTTP facade, `/openapi.json`                      | `agentplane/grpc/facade`, `proto_openapi`                                                                       |
| Workload identity (TokenReview)                             | `workload_auth/go/auth`; the `use-action-service` label through the `PolicyIndex` informers                     |
| PolicySet and PolicyBinding                                 | `policy/api/v1alpha1`, `egress/policy`, `egress/informer`; `ActionsReady` by server-side apply                  |
| NOTIFY wakeups                                              | `postgres/go/wakeups`                                                                                           |
| Database                                                    | `database/sql`, `lib/pq`, Goose; the Dispatcher store's patterns                                                |
| MCP server and client, resource-server auth, outbound OAuth | `modelcontextprotocol/go-sdk` (`mcp`, `auth`, `oauthex`), `golang.org/x/oauth2`                                 |
| Operator JWT (RS256, pinned issuer, audience, JWKS)         | `coreos/go-oidc/v3`                                                                                             |
| JSON Schema                                                 | `santhosh-tekuri/jsonschema/v6` or `google/jsonschema-go`; the stage that needs it picks one                    |
| Pod exec                                                    | client-go `tools/remotecommand`                                                                                 |
| Settings, logs, metrics, retry, single flight               | Viper with `ExperimentalBindStruct`, `slog` JSON, `prometheus/client_golang`, `go-retry`, `x/sync/singleflight` |

## The authorization server

External MCP clients authorize against the edge's authorization server, which runs in the Go binary beside `/mcp`
until the edge split. FastMCP's `OIDCProxy` does that in Python and no Go library does the same: `go-sdk` covers the
resource-server and client halves, and `fosite` and `zitadel/oidc` are protocol toolkits with no DCR or CIMD. The
operator chose a toolkit inside the service (2026-10-19 PDT) and then `zitadel/oidc` `op` over `fosite`
(maintained: `v3.51.16` on 2026-10-15, against `fosite`'s last tag in December 2024). Ory Hydra and a Python OAuth
edge were rejected.

## Contract

`action_service/action_service.proto` (`ActionService`) holds the RPCs, shapes and error codes that workloads and
operators share: the bearer picks the principal, and an operator reads every account's requests, the catalog with
MCP health detail, and each binding's diagnostics in `GetActionPolicy`, but submits and cancels nothing (operator,
2026-10-21: merge the reads). It carries what the edge needs to serve `/mcp` as a workload caller: a decided-only
submit with its own refusal code, `ActionGroup.direct_tools` plus the caller's auto-approvable Actions in
`GetActionPolicy`, each Action's title and MCP tool hints, and an optional subject on `GetActionPolicyRequest`. What
only an operator does is in three services, one per destination, so a later move changes no wire:
`OperatorActionService` (decisions, MCP server linkage), `agentplane.mcp_edge.ConnectionService` (Connections and
enrollments) and `WebPushService`. They live in files apart from `action_service.proto`, so the OpenAPI document
`proto_openapi` renders from it describes only what an agent may call. What the servicers and callers still owe the protos:

- `/openapi.json`, which `/llms.txt` already links, replaces `docs/agents/http_api.md` (stage L).
- The CLI's receipt wait moves to `WatchActionRequest`, which ends at the terminal or decision condition or the
  client's deadline, and `wait_seconds` and `wait_until` go (stage L).
- The app's Action watches now use the generated NDJSON facade through the generic proxy; the Python SSE relay,
  token-renewal wrapper and duplicate-snapshot filter are gone. Stage L remains in flight until the Go-backed
  aggregate acceptance exercises the caller path and live/suspended Sandbox compatibility is established.
- Hand-written HTTP stays: `/mcp`, the OAuth endpoints and well-known documents, `/oauth/client-metadata.json`,
  `/v1/mcp-linkage/callback`. `/healthz` and `/readyz` become `grpc.health.v1`; `/metrics` stays.
- The `_ERRORS` table becomes one interceptor mapping each domain error to a gRPC code and detail. Drain answers
  `UNAVAILABLE`. A revoked Connection is refused by `/mcp` itself, before any core call.

## Stored data the port must read and write exactly

"Schema unchanged" is not "stored data unchanged":

- Go neither reads nor writes `connection_enrollment`'s `decision_digest` and `browser_hash`, or
  `external_connection_grant`'s `request_digest` and `activation_deadline`: consent is pinned to its operator alone,
  any decision after an enrollment's first is refused, and a grant is created active in the transaction that issues
  its tokens, which nothing retries.
- `handle_hash` and `mcp_linkage_flow.state_hash` are `sha256` hex of the raw string, and the PKCE
  challenge is the unpadded URL-safe base64 of `sha256` of the verifier.
- `action_request.arguments`, `action_event.actor`, `action_execution.result` and `error`, and the scope lists are
  JSONB documents the Go store must read back and write in the same shapes.
- Keyset cursors (`(created_at, id)`, `(history_at, id)`) are opaque base64 and may change.

The first Go store stage generates golden vectors from the Python code (the History and Notification ports' method)
and asserts the Go values against them before any Go code writes to a shared database.

## Stages

Each stage is a small PR or a few, in dependency order. The service keeps serving from Python until the cutover.

| Stage | Change                                                                                                                                                                                                                                                                                                                                                                               |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| L     | Caller source migration is on the acceptance branch: the app uses generated facades, the CLI and Notification Service use generated clients, and acceptance tests use the new contract; the Python client and app Action routes/relay are removed. **Still in flight:** Go-backed aggregate acceptance and live/suspended Sandbox CLI compatibility evidence before testing rollout. |
| M     | Testing cutover: soak the Go migrator the roster publishes, roll the image with its callers, update the README system overview and `docs/system_overview.md`.                                                                                                                                                                                                                        |
| N     | Staging cutover, below.                                                                                                                                                                                                                                                                                                                                                              |
| O     | Python retirement: delete the Python service, tests, Alembic directory, migration image, `agentplane_oauth_kv`, `connection_enrollment`'s `decision_digest` and `browser_hash`, `external_connection_grant`'s `request_digest` and `activation_deadline` (a migration), the `fastmcp` pin and CI roster entries.                                                                     |

Stage O also deletes the Python-parity golden generators and tests (`generate_golden.py`, `generate_mcp_golden.py`,
`generate_mcp_executor_golden.py`, `generate_catalog_golden.py`, `policies/generate_golden.py` and their
`test_*golden.py`) and the Go golden tests' lists of deliberate differences; a `golden.json` stays only where its Go
test still earns it without the Python reference.

## Cutover

- **Testing, then staging**, with staging's image pins held while testing soaks, as for History.
- **Backup and restore first** ([staging data preservation](../docs/staging_data_preservation.md)). On the restored
  copy, the Go service's list and read answers equal the Python service's, field for field. No staging database is
  dropped or recreated.
- **Image before config.** The Go image accepts the current manifests' settings; probe, port and RBAC changes
  follow the pin. RBAC grants for the Go identity, including the `pods/exec` verb the Go client sends, land before
  the code needs them.
- **No overlap of Python and Go workers.** The roll uses `Recreate`: dispatch is single shot and a lease sweep
  from one side must not race the other's heartbeats. Web Push delivery too: Go's per-subscription advisory lock does
  not exclude Python's row lock, so two push loops, one of each, would both push to one browser.
- **External MCP clients re-authorize once**, at the flip and not again at the edge split. Their FastMCP-issued
  tokens cannot be honored. Connections and grants survive in the same tables and the existing reconnect path binds
  the new authorization to the old Connection after the operator confirms it. Say so in the cutover PR.
- **Workload Actions egress.** `BASIC_POLICY` in `cluster/cdk8s/agentplane/egress.py` already admits the full Actions
  service host and applies `agentplane-workload` Authorization bearer substitution, with no method or path filter.
  There is no path-only `/mcp` rule or matching assertion in `test_egress.py` to remove. Keep the host rule; Stage L
  still needs Go-backed CLI acceptance through the egress `CONNECT`/h2c path with the substituted workload token.
- **Live and suspended Sandboxes.** A Sandbox started before the cutover has the old CLI and guides. Its receipt
  wait still calls the removed HTTP wait parameters, so refresh its runner image/CLI before it resumes against Go.
  Inventory both live and suspended Sandboxes at stage L; a suspended Sandbox is not an active caller, but can resume
  with the stale CLI. The staging inventory on 2026-10-21 had three suspended Sandboxes and none running. This is the
  "decision card before PRs breaking live Sandboxes" case: carry that inventory into the cutover acceptance.

### After the flip: the edge split

1. The edge's database and a copy of its tables (database before code).
2. `actions` accepts ServiceAccount tokens that are not Pod-bound (reader before writer).
3. `mcp-edge`: `main`, Deployment, TokenRequest rights and token minting for the bound accounts, audience
   `actions`; the in-process `ActionService` call becomes the gRPC call.
4. The public route and the app's Connection calls move to the edge; `actions` stops being public.
5. The tables leave the `actions` database, in a separate data change.

## Risks

- **Authorization server correctness** is security-sensitive: real-client acceptance on testing before staging.
- **`go-sdk` states no API stability.** Pin the version and keep its use in the `/mcp`, executor and one auth file.
- **Tests.** Port the tests of our logic; `STYLE.md` says to delete library-basics tests, which drops a large
  share of the Python suite.

## Defaults taken

Change any of these by saying so; none holds a stage.

- A fix to the Python service is ported to the Go service in the same change, or waits for it.
- The MCP tools' bounded wait uses the same receipt stream in process, so there is one wait implementation.
