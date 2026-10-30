# Central sampler

An independent, opt-in runner type backed by its own PostgreSQL database. The sampler owns the
Responses API loop and lazily starts one shared pinned **Codex 0.162.1** JavaScript host for many
logical Sessions. `store()` / `load()` values persist across cells and idle turns in that host's
memory. Each code-mode cell runs in a fresh V8 isolate with no JavaScript globals shared between
cells, and provider continuation remains in the sampler. `notify()` outputs are queued durably and
enter the conversation where Codex would place them ([SPEC.md](SPEC.md)). Shell, stdin, patch, `view_image` and
filesystem operations go through Gateway to a Sandbox's loopback exec-server. Existing wrapped Codex and Claude
retain their current paths. The optional Agentplane cdk8s construct deploys the service when an
environment configures it; existing native Sessions are not migrated.

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
Gateway and History. The service and its shared JavaScript host use the Pod container's aggregate
CPU and memory limits.

Server-owned configuration names the service, the LLM proxy base URL, credential source,
model contracts and per-model reasoning-effort allowlists, TokenReview audience and allowed caller accounts,
code-mode host binary, executor-side Codex path, logs, concurrency/deadlines and limits. The LLM proxy connection
uses exactly one of `credential` or `credential_file`; the service reads the file before each
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

`max_live_sessions` bounds concurrent active Session drivers; it does not cap the number of stored
idle Sessions. The host starts lazily when code-mode work first needs it and multiplexes Sessions,
while executor delegates and process-handle namespaces remain per Session. Interrupting a Session
does not stop the shared host.

Host failure interrupts pending work for every attached Session; later code-mode work may start a
fresh host, but in-flight provider or executor effects are never replayed automatically. Configure CPU
and memory limits on the sampler container. Those limits cover the Go service and shared host, so
container exhaustion can interrupt every active Session. PostgreSQL retains durable conversation
and effect records, but host-memory `store()` / `load()` values and live JavaScript cells are lost
when the shared host exits.

TODO: Evaluate a stronger kernel-enforced isolation boundary for cells or Sessions if aggregate
container limits stop meeting the production resource contract.

## Agentplane opt in

- Gateway `central_samplers` maps deployment-owned names to gRPC targets and rotating caller token
  files. Its separate `executor_callers` allowlist authorizes sampler-to-executor access. Default
  empty settings preserve native operation and do not enable the new transport.
- The sampler allows the Dispatcher account for admission. History explicitly allows the sampler
  account in `ingress.sampler_accounts`; it retains the distinct executor Sandbox identity.
- The app's optional `model_catalog.central_sampler = {service}` and
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

## Model contracts

Only `api: responses` is implemented. A future API adapter (such as Anthropic Messages) can add
its own API-specific configuration alongside the shared exec encoding. Unsupported API values
are rejected. Provider-native transcript and continuation handling still require that adapter;
this configuration change does not implement cross-API session switching.

The top-level `models` map keys model route IDs to an API, tool encoding and allowed reasoning efforts.
Agentplane deployment configuration owns these compatibility choices; the shared model catalog
holds no sampler-specific fields. The service uses one top-level proxy connection; upstream
routing belongs to the LLM proxy:

```json
{
  "llm_proxy_url": "http://llm-proxy.invalid",
  "credential_file": "/var/run/secrets/llm/token",
  "models": {
    "native-code-model": {
      "api": "responses",
      "responses": { "dialect": "lite" },
      "exec_encoding": "freeform",
      "reasoning_efforts": ["", "high"]
    },
    "function-calling-model": {
      "api": "responses",
      "responses": { "dialect": "standard" },
      "exec_encoding": "json",
      "reasoning_efforts": [""]
    }
  }
}
```

`responses.dialect` and `exec_encoding` are independent; all four combinations are supported. `lite` uses Codex's
Responses Lite header, `additional_tools` input item and all-turn reasoning context. `standard`
declares top-level Responses `tools` and omits the private Lite header, reasoning context and
client metadata. With no reasoning effort selected, standard requests omit reasoning configuration
and encrypted-reasoning includes entirely.

`freeform` exposes the native JavaScript `exec` custom tool and separate function `wait`.
`json` exposes only a function `exec`: pass `{code, yield_time_ms?, max_output_tokens?}` to execute
JavaScript, or `{cell_id, yield_time_ms?, max_output_tokens?, terminate?}` to resume or terminate a
yielded cell. Both use the same JavaScript host, nested tools, output limits and effect journal.
Standard requests send text-only function results as strings and multimodal results as content
arrays. Asynchronous `notify()` output becomes a developer message identifying its original call,
so the public API receives only one tool result per call.

Staging combines Agentplane-owned compatibility policy with shared model reasoning metadata.
A model change within a Session must preserve API, dialect and exec encoding, and the target must
allow the current reasoning effort. Treat a route's contract as fixed while its Sessions exist;
use a new route ID to change it. Standard routes reject manual compaction, and reaching the
configured automatic compaction threshold fails the turn before any private compaction request.
Portable compaction is not implemented. Provider compatibility still needs live validation;
scripted tests establish the sampler's protocol and execution behavior.

Rollback: remove the app offerings and named central route, then disable the sampler's inbound
allowlists. Preserve the sampler DB and History records. Reverting sampler code does not convert a
central Session into a native harness Session.
