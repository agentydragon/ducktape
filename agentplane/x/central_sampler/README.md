# Central sampler

An independent, opt-in runner type backed by its own PostgreSQL database. The sampler owns the
Responses and native Anthropic Messages loops and lazily starts one shared pinned **Codex 0.162.1** JavaScript host for many
logical Sessions. `store()` / `load()` values persist across cells and idle turns in that host's
memory. Each code-mode cell runs in a fresh V8 isolate with no JavaScript globals shared between
cells, and provider continuation remains in the sampler. `notify()` outputs are queued durably and
enter the conversation where Codex would place them ([SPEC.md](SPEC.md)). Shell, stdin, patch, `view_image` and
filesystem operations use the typed `Executor` API. The service terminates at the Sandbox's Runner;
Gateway proxies that same contract when production routing and caller authorization are needed.
The Runner's sandbox-local adapter connects it to the loopback exec-server. Existing wrapped Codex and Claude
retain their current paths. The optional Agentplane cdk8s construct deploys the service when an
environment configures it; existing native Sessions are not migrated.

The service, sampling loop, persistence and exporter are implemented in Go. The JavaScript host is
the pinned upstream Codex binary. Python is used only by the acceptance fixtures.

[The API](sampler.proto) is usable without the integration app. [SPEC.md](SPEC.md) defines admission,
recovery and evidence.

The sampler is a service Agentplane owns rather than upstream Codex's app-server moved out of the
Sandbox: owning the provider loop puts every request, response and tool effect in its database
([spike evidence](../../debug/postgres_codex_spike.md)). Wrapped harnesses Agentplane does not own,
official Claude and native Codex, stay independently usable backends; no sampler success is a
reason to retire them or migrate their state. Backends expose different facts, so History keeps
each backend's raw evidence and opaque continuation without inventing absent details; normalized
Events extend the shared projection and do not claim a lossless transcript.

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

Server-owned configuration names the service, provider connections and credential sources,
model contracts and per-model reasoning-effort allowlists, TokenReview audience and allowed caller accounts,
code-mode host binary, executor-side Codex path, logs, concurrency/deadlines and limits. Each named connection uses `auth.type` (`bearer` or `anthropic_api_key`) and exactly one of
`auth.credential` or `auth.credential_file`; the service reads the file before each request so projected service-account tokens can rotate. `AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL`
overrides `database_url` in the JSON settings, so database credentials can stay in a Secret-backed
environment variable. Gateway mode requires `gateway_target` and a rotating projected
`gateway_token_file`. The sampler uses one generated `Executor` client for its Gateway and direct
local-test targets. It keeps one `OpenSession` gRPC stream to Runner and sends process and file
operations as typed unary RPCs. Runner owns the connection to Codex's exec-server; the gRPC stream
is not the Codex session itself. Closing the stream releases that adapter connection without
sending a process-termination request. Pinned Codex 0.162.1 keeps a detached native session for up
to 30 seconds, then expires it and terminates processes still running in it. The adapter adds no
durable session storage or new process policy. Unary request IDs correlate with the
sampler's durable intent journal; they do not deduplicate or cache server results. Configure
`gateway_target` as `{sandbox}-control.{namespace}.svc:8080` for the per-Sandbox control Service:
it selects the Gateway replica holding that Runner's observation Lease. A load-balanced Gateway
Service is unsuitable when there are multiple replicas. Fixed targets support single-process
fixtures. Templates accept DNS-label executor names and only these two placeholders. A fixed
`direct_executor_target` supports standalone fixtures without Gateway only when
`direct_executor_insecure_localtest` is explicitly true and the target is a loopback IP address.
Choose exactly one Gateway or direct execution target. Production execution uses Gateway bearer
authentication.
Optional `history_target` and `history_token_file` enable retained History export.

`max_live_sessions` bounds concurrent active Session drivers; it does not cap the number of stored
idle Sessions. The host starts lazily when code-mode work first needs it and multiplexes Sessions,
while executor delegates and process-handle namespaces remain per Session. Interrupting a Session
does not stop the shared host. The Executor adapter separately limits each Runner to eight open
or opening streams; that is a connection limit, not a limit on stored sampler Sessions or the
sampler's `max_live_sessions`. See the [Executor API resource and lifecycle contract](../../executor/README.md).

Host failure interrupts pending work for every attached Session; later code-mode work may start a
fresh host, but in-flight provider or executor effects are never replayed automatically. Configure CPU
and memory limits on the sampler container. Those limits cover the Go service and shared host, so
container exhaustion can interrupt every active Session. PostgreSQL retains durable conversation
and effect records, but host-memory `store()` / `load()` values and live JavaScript cells are lost
when the shared host exits.

TODO: Evaluate a stronger kernel-enforced isolation boundary for cells or Sessions if aggregate
container limits stop meeting the production resource contract.

## Agentplane opt in

- Gateway `central_sampler` configures one gRPC target and rotating caller token file. Its separate `executor_callers` allowlist authorizes access to the same `Executor` API the Runner serves inside the Sandbox. Default
  empty settings preserve native operation and do not enable the new transport.
- The sampler allows the Dispatcher account for admission. History explicitly allows the sampler
  account in `ingress.sampler_accounts`; it retains the distinct executor Sandbox identity.
- The app's `HARNESS_CENTRAL_SAMPLER` model list offers **Central sampler** alongside Codex and Claude.
  App and Actions callers do not supply service names or endpoints. A non-empty setup script runs once on the bound executor, in the resolved working directory,
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
Responses Lite compaction uses Codex's remote-v2 beta protocol. Anthropic uses native server-side
on-demand compaction; choose a supporting model. The manual
`//agentplane/x/central_sampler:test_live_anthropic_provider.live` target exercises real organization
API access, strict JSON exec, signed thinking and compaction across restart, and Haiku cache
creation/reads through an effort change. BuildBuddy must supply `ANTHROPIC_API_KEY` securely;
[persistent provisioning remains a follow-up](https://github.com/agentydragon/ducktape/issues/13063).
The automated cache test uses ordinary sampler instructions and tools without padding, and
asserts positive provider-reported cache counts plus stable system, tool and message prefixes.
A separate staging canary also verified reuse on unpadded inputs after an effort change.
See the [Anthropic acceptance record](docs/anthropic_acceptance.md)
for the test scope and observed rollout, and the [v1 validation evidence](../../debug/central_sampler_v1.md)
for earlier fixture diagnosis.

## Model contracts

The `models` map binds public route IDs to a named `connection`, explicit `provider_model`,
API contract and reasoning-effort allowlist. Connections belong to server settings; Session callers
cannot choose endpoints or authentication. The organization Anthropic connection goes directly to
`api.anthropic.com`, independently of the Responses proxy. Both providers use the same V8 host,
nested tools, executor, effect journal and History export.

```json
{
  "connections": {
    "responses-ingress": {
      "base_url": "http://llm-proxy.invalid",
      "auth": { "type": "bearer", "credential_file": "/var/run/secrets/llm/token" }
    },
    "anthropic-organization": {
      "base_url": "https://api.anthropic.com",
      "auth": { "type": "anthropic_api_key", "credential_file": "/var/run/secrets/anthropic/api-key" }
    }
  },
  "models": {
    "native-code-model": {
      "connection": "responses-ingress",
      "provider_model": "native-code-model",
      "api": "responses",
      "responses": { "dialect": "lite" },
      "exec_encoding": "freeform",
      "reasoning_efforts": ["", "high"]
    },
    "anthropic-api/ant-messages/claude-haiku-5-5": {
      "connection": "anthropic-organization",
      "provider_model": "claude-haiku-5-5",
      "api": "anthropic_messages",
      "exec_encoding": "json",
      "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
      "anthropic_messages": {
        "max_tokens": 16384,
        "thinking": { "type": "adaptive", "block_binding": { "prefix_mismatch_behavior": "error" } },
        "prompt_cache": { "ttl": "5m" },
        "cache_diagnostics": true,
        "per_message_effort": true,
        "strict_tool_input": true,
        "eager_input_streaming": true,
        "compaction": { "max_tokens": 4096, "auto_compact_tokens": 160000, "keep_recent_turns": 1 }
      }
    }
  }
}
```

The Go implementation separates native protocols into `anthropic/` and `responses/` packages.
Both depend on `protocol/` for content, tool invocations, context operations and the evidence sink;
neither imports the sampler or the other API package. The sampler owns settings, credentials,
leases, effect intents, execution and storage. Native compaction plans prepare requests and validate
replacements; the sampler commits the request before dispatch and applies the replacement under
its context and lease fences.

Conversations record their API explicitly. Responses stores native Responses items; Anthropic
stores native Messages API messages. Typed protocol contexts own rendering, tool pairing, effort,
and compaction. Generic tool invocations carry native source-record links and are execution
projections, not additional conversation history. Both APIs share the durable effect journal and
History export.

Anthropic preserves complete native messages, signed thinking/redacted thinking and signed
compaction blocks. Eager argument deltas are logged as they arrive; no tool is executed until
`message_stop` validates a complete `tool_use` response. Truncation before that terminal event
leaves the intent unknown. Unsupported blocks and stop reasons fail without executing partial tools.
Images use base64 image blocks, and repeated asynchronous outputs become contextual user messages.

The adapter sends a stable system prompt and tool schema. Environment and execution-handle changes
are appended durably. Effort changes append an empty system control using Anthropic's beta, keeping
the launch effort fixed at the top level. Cache diagnostics compare against the last ordinary
provider response ID, including across sampler restarts. Cache counters and full native diagnostics
are retained in the DB; an unavailable diagnostic is not a cache miss.

`compaction` enables manual and sampler-triggered server-side on-demand compaction. It replaces only
active context, leaving the append-only conversation and History intact. `keep_recent_turns: 0`
summarizes all context; positive values preserve that many completed turns and any subsequent input.
Boundaries move backward over an effort control to keep signed thinking valid. The signed summary
goes first, followed directly by the unchanged suffix. Summarized effort is restored after the next
new user message. Live cells and unanswered tools prevent compaction; failed compaction preserves
old context. Background compaction and dynamic definitions remain in the [task DAG](../../plans/task_dag.md).
Langfuse export is an independent deferred follow-up; native exchanges are already retained in
the sampler DB.

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

Session creation accepts harness and model choices. The Dispatcher uses its one configured central
sampler target; the app catalog, browser, Actions inputs and stored session specs carry no routing
metadata.

Staging offers official OpenAI ChatGPT routes through wrapped Codex and third-party coding models
through the central sampler with standard Responses and JSON exec. Adding a third-party route to
Agentplane's coding-model selection automatically gives it that contract. The Haku preset uses
the central sampler for Qwen; existing sessions keep their harness. Backend model contracts and
Codex metadata remain available for existing sessions.

Staging combines Agentplane-owned compatibility policy with shared model reasoning metadata.
A model change within a Session must preserve connection, API, dialect and exec encoding; Anthropic
also requires the same provider model and request options because signed continuation is bound to
them. The target must
allow the current reasoning effort. Treat a route's contract as fixed while its Sessions exist;
use a new route ID to change it. Standard routes reject manual compaction, and reaching the
configured automatic compaction threshold fails the turn before any private compaction request.
Portable compaction is not implemented. Provider compatibility still needs live validation;
scripted tests establish the sampler's protocol and execution behavior.

Rollback: remove the app offerings and named central route, then disable the sampler's inbound
allowlists. Preserve the sampler DB and History records. Reverting sampler code does not convert a
central Session into a native harness Session.

## Codex features left out

- Sandboxing, execpolicy, approvals and the network proxy: the Sandbox, egress proxy and Action
  Service own these boundaries.
- Codex local persistence (rollout files, thread store, state DB): PostgreSQL and History replace it.
- Turn diffs, review mode, desktop in-app panes and fast mode.
- Codex plugins, apps and connectors, unless specifically wanted later.
- Image generation: a different API; if wanted, an Agentplane Action instead.
- Shell plumbing (shell snapshots, zsh fork, login-shell PATH, PowerShell), worktree management and
  external-agent migration.
- TUI, cloud tasks and ChatGPT-backend features (login/keyring, PSP, plan analytics, access
  programs, request compression).
- Local model providers (LiteLLM routes models), Codex's OTel exporter, feedback upload and Windows
  support.
