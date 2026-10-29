# Central sampler

An independent, opt-in runner type backed by its own PostgreSQL database. The sampler owns the
Responses API loop and runs the real pinned **Codex 0.162.1** JavaScript host beside it. Shell,
stdin, patch and filesystem operations go through Gateway to a Sandbox's loopback exec-server.
Existing wrapped Codex and Claude retain their current paths. Nothing here deploys the service,
changes existing Sessions, or migrates native harness state.

The service, sampling loop, persistence and exporter are implemented in Go. The JavaScript host is
the pinned upstream Codex binary. Python is used only by the acceptance fixtures.

[The API](sampler.proto) is usable without the integration app. [SPEC.md](SPEC.md) defines admission,
recovery and evidence; the [sampling plan](../../plans/sampling_outside_sandboxes.md) records the
placement decision and deferred Claude branch.

For a blocked Session, use the [recovery guide](docs/blocked_session_recovery.md) to inspect its
reason and linked evidence without replaying an uncertain operation.

## Running

Build `//agentplane/x/central_sampler:main_bin`. Supply a JSON file matching
[`Settings`](settings.go), then run the binary with `--config PATH --initialize` to apply its
Goose SQL baseline. Run with `--config PATH` to serve. Use a PostgreSQL URL (`postgresql://`). No
schema is created implicitly on server startup. Initialize a dedicated empty DB. Repeating
initialization retains Session identities, conversation state, evidence and outbox.

`//agentplane/x/central_sampler/cmd:service_image` packages the service and pinned code-mode host.
The [k3s acceptance scenarios](../../system_tests/README.md#central-sampler) run that image in its
own Pod, initialize its DB with an init container, and use projected service-account tokens for
Gateway and History. The sampler and all of its JavaScript hosts share the Pod container's
aggregate CPU and memory limits.

Server-owned configuration names the service, provider routes (endpoint, credential placeholder,
model and reasoning-effort allowlists), TokenReview audience and allowed caller accounts,
code-mode host binary, executor-side Codex path, logs, concurrency/deadlines and limits. Providers
use exactly one of `credential` or `credential_file`; the service reads the file before each
request so projected service-account tokens can rotate. `AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL`
overrides `database_url` in the JSON settings, so database credentials can stay in a Secret-backed
environment variable. Gateway mode requires `gateway_target` and a rotating projected
`gateway_token_file`. Configure
`gateway_target` as `{sandbox}-control.{namespace}.svc:8080` for the per-Sandbox control Service:
it selects the Gateway replica holding that Runner's observation Lease. A load-balanced Gateway
Service is unsuitable when there are multiple replicas. Fixed targets support single-process
fixtures. Templates accept DNS-label executor names and only these two placeholders. A fixed
`direct_executor_url` supports standalone fixtures without Gateway; choose exactly one transport.
Optional `history_target` and `history_token_file` enable retained History export.

`max_live_sessions` bounds the number of concurrent Session hosts. The service samples each host
leader's RSS every 100 ms and kills its process group when the configured `host_rss_bytes` limit is
exceeded. This is a supervisor guard, not a kernel-enforced per-host memory limit; descendants are
not included in the RSS sample. Configure CPU and memory limits on the sampler container. Those
limits cover the Go service and every live host together, so container memory exhaustion can stop
all Sessions in the Pod. PostgreSQL retains durable conversation and effect records, but live
JavaScript cells cannot be restored after the service or host exits. Ordinary shutdown kills a
host's process group; descendants that create a new session may remain until the container stops.

TODO: Revisit a delegated per-host kernel boundary if shared container limits stop meeting the
production resource contract.

## Agentplane opt in

- Gateway `central_samplers` maps deployment-owned names to gRPC targets and rotating caller token
  files. Its separate `executor_callers` allowlist authorizes sampler-to-executor access. Default
  empty settings preserve native operation and do not enable the new transport.
- The sampler allows the Dispatcher account for admission. History explicitly allows the sampler
  account in `ingress.sampler_accounts`; it retains the distinct executor Sandbox identity.
- The app's optional `model_catalog.central_sampler = {service, provider}` and
  `HARNESS_CENTRAL_SAMPLER` model list offer **Central sampler** alongside Codex and Claude. A caller
  cannot choose arbitrary service endpoints. A non-empty setup script runs once on the bound executor, in the resolved working directory,
  before sampling starts. Its stdout, stderr and terminal outcome are retained in History.
- LLM ingress must allow the sampler account in `central_sampler_accounts` to accept its
  `x-agentplane-session-id` claim. Ingress validates a canonical UUID and generates trusted
  metadata. Langfuse links filter by public Session ID; Sandbox identity describes execution.

Readiness for the first open still requires a running executor. Binding is fixed to Sandbox UID
and incarnation. A subsequent missing/suspended executor fails tool connection or execution;
central controls and persisted conversation remain on the sampler. Dispatcher commands still need
the extant bound Sandbox object; the standalone API can inspect/control its own persisted Session.

## Verification

Run the service, exporter and executor integration targets via Bazel. They use real PostgreSQL,
pinned upstream host/executor processes, authenticated Gateway/History and scripted provider wire
fixtures. Scripted acceptance does not establish live provider or subscription compatibility.
Provider compaction uses Codex's remote-v2 beta protocol; choose a supporting endpoint/model.
See the [v1 validation evidence](../../debug/central_sampler_v1.md) for results and fixture diagnosis.

Rollback: remove the app offerings and named central route, then disable the sampler's inbound
allowlists. Preserve the sampler DB and History records. Reverting this code does not convert a
central Session into a native harness Session. No deployment or database deletion is part of this PR.
