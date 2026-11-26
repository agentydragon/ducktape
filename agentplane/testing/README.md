# Agentplane test helpers

Shared fakes, fixtures and launchers for Agentplane tests. Which tier a test belongs in (unit, component, harness,
system, live): [test tiers](../docs/test_tiers.md). Repo-wide helpers (PostgreSQL container, OIDC mock, ASGI,
browser): [`//testing`](../../testing/README.md).

Check this index before writing a new fake or launcher; add a line when you add a shared helper. Shared helpers live
here or in the owning service's `testing/`.

## Naming

- **Python launcher of a built binary:** `<binary>_process.py` defining `<Binary>Process` (`gateway_process.py` → `GatewayProcess`). Shared base: `go_process.py`.
- **Python pytest fixture module:** `fixtures.py` when it holds the package's fixtures, `<topic>_fixtures.py` beside other topics.
- **Go helper library:** `<pkg>test` for helpers serving one package's consumers, as `net/http` → `httptest` (`servicetest`, `configtest`); otherwise a plain noun for what it provides (`postgres`, `grpcserver`). Never a `fixture` suffix: `testing/` already says test.
- **Go helper binary** (`package main`): snake_case directory named for its role (`serving_fixture`, `runtime_probe`).

## Python (`agentplane/testing/*.py`)

- [`go_process.py`](go_process.py): run a built Go service as a child; `go_process` waits for gRPC health SERVING,
  stops with TERM + grace (SIGQUIT goroutine dump on overrun), asserts exit 0. `GoProcess.paused()` simulates an
  outage; `restartable_go_process` restarts with a callback while stopped.
- [`kubernetes_apiserver.py`](kubernetes_apiserver.py): real kube-apiserver + etcd (envtest, Agentplane CRDs, no
  scheduler/kubelet). `control_plane()` loop-free, session-shareable; `kubernetes_api()` per-loop client;
  `kubernetes_apiserver()` a control plane of one test's own. `KubernetesApiServer.pod_token` mints Pod-bound SA
  tokens; `dry_run_admitted`/`wait_until_refused` for admission policy.
- [`kubernetes_apiserver_fixtures.py`](kubernetes_apiserver_fixtures.py): `kubeconfig` (session) + `api_server`
  (per test) fixtures over the above; import both by name into a conftest.
- [`crd_admission.py`](crd_admission.py): `crd_namespace()` → `CrdAdmission` per kind; create/replace spec/status
  and `rejects_*` asserting a 422 from the API server's schema + CEL.
- [`kubernetes.py`](kubernetes.py): `CustomObjectsClient` Protocol, typed cast of the dynamic custom-object API (adds
  `_content_type` for JSON Patch).
- [`sandbox_fixtures.py`](sandbox_fixtures.py): `sandbox` fixture, a recorded Sandbox + runner Pod on `api_server`
  (via `sandboxes/testing/kubernetes.py`).
- [`native_gateway_fixtures.py`](native_gateway_fixtures.py): real Dispatcher + Environments (`gateway_service`),
  envtest, Sandbox and PostgreSQL fixtures for native contracts (`dispatcher`, `runtime_client`, `control`).
- [`native_service_fixtures.py`](native_service_fixtures.py): production service composition without the app or an
  in-test cluster: above + Go History Service, History ingress, `start_runtime` (Runtime in a pid1 container).
- [`scripted_model.py`](scripted_model.py): harness-neutral loopback model; tests script `Text`/`Reasoning`/`ShellCall`
  and read `ModelRequest`, so one test body runs against both harnesses.
  - [`claude_model.py`](claude_model.py): `ClaudeModel`, Anthropic Messages dialect.
  - [`codex_model.py`](codex_model.py): `CodexModel`, OpenAI Responses dialect.
- [`codex_fixtures.py`](codex_fixtures.py): pinned `codex` / `codex-code-mode-host` runfiles paths,
  `codex exec-server` executor process, temporary-database `database_url`.
- [`tls.py`](tls.py): throwaway CAs (`make_ca`), server leaves (`issue_leaf`), workload mTLS leaves with URI + DNS SANs
  (`issue_workload_leaf`, `workload_uri`), SSL contexts.
- [`electric_role.py`](electric_role.py): `ensure_electric_role`, create the cluster-wide Electric login before
  migrations.

## Go (`agentplane/testing/<pkg>`)

- [`kubeapiserver`](kubeapiserver/): Go counterpart of `kubernetes_apiserver.py`; `Run` (TestMain), `StartForTest`,
  `WithCRDs`/`WithAPIServerFlag`, Pod-bound SA tokens.
- [`envtest`](envtest/): the `server` binary `kubernetes_apiserver.py` launches (envtest assets + CRDs → kubeconfig).
- [`postgres`](postgres/): real PostgreSQL container migrated by the service's Goose code; `RunIntegrationTests`
  (`…WithCloneTemplate` for pristine clones), `Open`, `Count`, lock-wait helpers (`BackendPID`, `WaitForBlocker`),
  `EnsureElectricRole`.
- [`wakeups`](wakeups/): LISTEN on PostgreSQL notification channels; `Observe` reports which channels a step woke.
- [`grpcserver`](grpcserver/): listeners/connections for test gRPC servers; `ServeRegistered`, `ConnectInMemory`.
- [`grpcmethods`](grpcmethods/): call every RPC of a `grpc.ServiceDesc` by name to check `auth.Methods` caller
  policy; `ServeTLS` + `Caller`/`AsCaller` per client cert; `WithActionDelegation` delegation envelope.
- [`executor`](executor/): fake generated `executorpb` client with scripted streams (completed, failed, late, gated).
- [`eventually`](eventually/): polling assertions `Eventually`, `Within`, `PathExists`.
- [`httptransport`](httptransport/): `RoundTripperFunc`; `WatchStreams` to close selected watch response bodies.
- [`configtest`](configtest/): `Load` with prefix-scrubbed env; `WriteSettings` writes a temp `settings.yaml`.
- [`testscheme`](testscheme/): Kubernetes scheme with built-ins + Sandbox, policy, WorkloadCredential and KubeVirt
  kinds (Environments tests).
- [`jsonassert`](jsonassert/): JSON equality without number rounding.
- [`yamltest`](yamltest/): `JSONInput`, normalize JSON so yaml.v3 sees code points, not surrogate pairs.

## Per-service helpers used across services

Only `testing/` packages with importers outside the owning service (checked by import/BUILD dep). Other
`<svc>/testing/` dirs are local to their service.

- `history_service/testing/`
  - [`go_service.py`](../history_service/testing/go_service.py): run the built Go History Service
    (`go_history_service`), `migrated_history_template`, `wait_for_history_session`/`…_snapshot`.
  - [`fixtures.py`](../history_service/testing/fixtures.py): pytest fixtures over it (`history_service`,
    `service_history_db_url`).
  - [`history.py`](../history_service/testing/history.py): `scripted_history`, write a Session's entries as a runner
    would, read back folded rows.
  - [`electric_service.py`](../history_service/testing/electric_service.py): real Electric over a history database with
    the deployment's restricted role.
  - [`session_events.py`](../history_service/testing/session_events.py),
    [`unstored_ranges.py`](../history_service/testing/unstored_ranges.py),
    [`protojson.py`](../history_service/testing/protojson.py): correction-aware event paging, unstored ranges,
    ThreadEntity ProtoJSON.
- `environments/testing/` ([README](../environments/testing/README.md))
  - [`environment_service_process.py`](../environments/testing/environment_service_process.py): production EnvironmentService (+ optional Runtime proxy) in a
    child process.
  - [`servicetest`](../environments/testing/servicetest/): production EnvironmentService handlers for Go
    consumer tests (Actions orchestration).
- `session_dispatcher/testing/`
  - [`gateway_process.py`](../session_dispatcher/testing/gateway_process.py): Sessions + its Environments peer against
    a supplied API server and PostgreSQL (`run_gateway`).
  - [`runner_server.py`](../session_dispatcher/testing/runner_server.py): scripted Runtime served to the Environments
    proxy over mTLS.
- `sandboxes/testing/`
  - [`kubernetes.py`](../sandboxes/testing/kubernetes.py): `sandbox_cluster`, a Sandbox + runner Pod recorded as the
    Controller would; statuses are what the test writes.
  - [`sandbox_namespace.py`](../sandboxes/testing/sandbox_namespace.py): one test's namespace and the objects Sandbox
    readers find there.
  - [`identity.py`](../sandboxes/testing/identity.py): `SandboxIdentity` (namespace, name, UID).
- `env/e2e/testing/`: runner launch and drive. [`launches.py`](../env/e2e/testing/launches.py) +
  [`config.py`](../env/e2e/testing/config.py) (pinned-binary config), [`fixtures.py`](../env/e2e/testing/fixtures.py)
  (harness-parametrized `model`/`endpoint`/`spec`), [`process.py`](../env/e2e/testing/process.py) /
  [`process_fixtures.py`](../env/e2e/testing/process_fixtures.py) (runner subprocess, pid1 container),
  [`gateway.py`](../env/e2e/testing/gateway.py) (Runner control listener client),
  [`driver.py`](../env/e2e/testing/driver.py), [`events.py`](../env/e2e/testing/events.py) (Event assertions),
  [`history_ingress.py`](../env/e2e/testing/history_ingress.py),
  [`model_config.py`](../env/e2e/testing/model_config.py),
  [`runtime_with_history.py`](../env/e2e/testing/runtime_with_history.py),
  [`failure_journal.py`](../env/e2e/testing/failure_journal.py) (runner evidence on failure),
  [`claude_transcript.py`](../env/e2e/testing/claude_transcript.py).
- [`env/runtime/execution/testing/executor_companion.py`](../env/runtime/execution/testing/executor_companion.py):
  execution backends of the Runtime companion container.
- `native/testing/`, `native/{claude,codex}/testing/`: [`model_endpoint.py`](../native/testing/model_endpoint.py)
  (typed loopback model endpoint), per-harness wire endpoints (`messages.py`, `responses.py`), SSE builders
  (`anthropic_sse.py`, `responses_sse.py`), request models (`requests.py`), test model names (`model.py`), Codex
  `frames.py` and `model_catalog.py`. Wrapped by `scripted_model.py` above.
- `app/testing/`: [`client.py`](../app/testing/client.py) (Go app HTTP client, used by system tests),
  [`models.py`](../app/testing/models.py), [`history_reads.py`](../app/testing/history_reads.py),
  [`gateway_fixtures.py`](../app/testing/gateway_fixtures.py) and
  [`runtime_fixtures.py`](../app/testing/runtime_fixtures.py) (also used by `session_dispatcher`).
- [`action_service/testing/`](../action_service/testing/README.md): Dex-backed OAuth MCP server image for k3s system
  tests.
- Go:
  - [`net/peertls/testing`](../net/peertls/testing/): short-lived mTLS CAs and leaves; most Go services' tests.
  - [`identity/auth/testing`](../identity/auth/testing/): live Sandbox workload credentials on an envtest API server.
  - [`identity/credentials/testing`](../identity/credentials/testing/): plays the workload-identity issuer and
    cert-manager for requesters' tests.
  - [`sampler/testing/exporttest`](../sampler/testing/exporttest/): TESTONLY view of the sampler sessionstore
    outbox for History integration tests.
