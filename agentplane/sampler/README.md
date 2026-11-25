# Sampler

An independent, opt-in runner type backed by its own PostgreSQL database. The sampler owns the
Responses, Chat Completions, and native Anthropic Messages loops and lazily starts one shared pinned **Codex 0.171.0** JavaScript host for many
logical Sessions. `store()` / `load()` values persist across cells and idle turns in that host's
memory. Each code-mode cell runs in a fresh V8 isolate with no JavaScript globals shared between
cells, and provider continuation remains in the sampler. `notify()` outputs are queued durably and
enter the conversation where Codex would place them ([SPEC.md](SPEC.md)). Shell, stdin, patch, `view_image` and
filesystem operations use the typed `Executor` API. The service terminates at the Sandbox's Runner;
Environments proxies that same contract when production routing and caller authorization are needed.
The Runner's sandbox-local adapter connects it to the loopback exec-server. Existing wrapped Codex and Claude
retain their current paths. The optional Agentplane cdk8s construct deploys the service when an
environment configures it; existing native Sessions are not migrated.

Central sampler V1 is selected by `SessionSpec.placement=central_sampler_v1` and requires one exact,
fixed executor environment; [SPEC.md](SPEC.md#identity-and-admission) defines its admission.
The placement-owner RPC is `HarnessHost.CreateSession`; it receives the committed placement and retains
these V1 admission semantics. `Executor.OpenSession` is separate: its stream owns one execution
connection to the Runner and does not create the logical Session.

The shared child process, wire framing, request routing and logical-session lifecycle live in
`internal/codemode`. The sampler binds each session to its lease-fenced journal and delegated tool
handler; model tool adaptation, nested executor intents and result evidence remain sampler-owned.
The `internal/executor` facade owns Store settlement, process cursors and model-visible tool results.
Its `internal/executor/transport` package owns the Runner connection, session handshake/rebind and
typed process and file RPCs; narrow bindings keep durable session transitions in the Store.

The service, sampling loop and persistence are implemented in Go. History export lives in the
`historyexport` package and reaches sampler state through its durable outbox interface. The
JavaScript host is the pinned upstream Codex binary. Python is used only by the acceptance fixtures.

`internal/sessionstore` owns durable Session state, transactions, request preparation and recovery.
The runtime supplies a concrete provider-request recipe; the Store prepares and commits it under
the existing ownership and context fences before the runtime performs network I/O. History's
integration tests use the sampler-owned `testing/exportfixture` source without importing Store
internals.

`internal/residentruntime` owns replica scheduling, capacity, recovery scans and coordinated drain.
Its `session` package owns each resident driver, provider I/O, the Code Mode binding and lease
claim, heartbeat and release. A scheduler-owned slot coordinates each driver with the replica;
the scheduler joins all drivers before closing the shared host. `internal/rpc` owns authentication/admission, status mapping and request/event
streaming. `cmd` composes these concrete owners and the History exporter. Public RPC, provider
protocol and export contracts remain in `api`, `protocol`, `providers` and `historyexport`; there
is no Go implementation package at the service root.

The [HarnessHost API](../harness_host/harness_host.proto) and
[SamplerAdmin API](api/sampler_admin.proto) are usable without the integration app.
`service_callers` grants the lifecycle and command API;
`admin_service_callers` separately grants diagnostics, provider-request reads and recovery. Both
use the same mTLS listener and retain the exact caller SPIFFE ID for Session ownership.
[sampler.proto](api/sampler.proto) retains internal command and status types and the legacy creation
request shape needed to decode stored configuration; it declares no gRPC service.
[SPEC.md](SPEC.md) defines admission, recovery and evidence.

Agentplane owns the sampler's provider loop and stores provider and tool-effect evidence in its
database. Official Claude and native Codex remain independently usable backends with their existing
state. History keeps each backend's raw evidence and opaque continuation. Normalized Events project
the facts each backend exposes.

For a blocked Session, use the [recovery guide](docs/blocked_session_recovery.md) to inspect its
reason and linked evidence without replaying an uncertain operation.

## Running

Build `//agentplane/sampler/cmd:main_bin`. Supply a JSON file matching the Go
[`Settings`](internal/settings/) package, then run the binary with `--config PATH` to serve. Use a PostgreSQL URL
(`postgresql://`). Startup applies Goose migrations before opening the service database connection
or starting workers. Baseline 15 creates the current schema on an empty database and adopts fully upgraded
version-14 databases after checking their history and schema. Adoption preserves Session records,
conversation state, evidence, and export checkpoints. Older or partial histories must first finish
migrating with the previous image.
For an explicit migration-only run, set `AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL` and pass
`--migrate-only` as the sole argument. Normal service startup performs the same migration before
it serves.

Migration 21 adds the sampler journal's block table. With `event_block_writes` enabled, the
writer seals completed 32-cursor windows in the append transaction. Each block compresses up to
32 consecutive exact event frames and decodes to at most 2 MiB. The live tail, oversized frames,
and groups that do not benefit from compression stay in standalone rows. The
[historical packing Job](#historical-journal-packing) can pack earlier completed windows.
The exporter and recovery read both forms.

Deploy the block-capable image across all replicas with `event_block_writes` omitted before
enabling it. To roll back block writes, remove the setting and roll the block-capable image
again. Check sampler event follow, provider recovery, and History export checkpoints after the
roll. Keep migration 21 and the block-capable reader as the rollback floor while blocks exist.

`context_source_writes` enables sourced context entries. Deploy the reader to every sampler
replica before enabling writes. With the setting omitted, the sampler reads both formats and
writes bare native records. After enabling writes, keep a reader that understands sourced context
entries as the rollback floor. Disabling writes and rolling that reader preserves existing
entries and stops adding sources to new records.

Migration 16 adds required durable provider-attempt metadata to every sampling and compaction intent.
It preserves existing Sessions, conversations, raw evidence, and History export checkpoints, but the
new constraint prevents an older sampler image from inserting provider intents. Staging uses its
inherited rolling profile. To roll back to a pre-migration image, stop new writers and restore a
pre-migration database backup before starting it; an image-only rollback cannot write provider
intents under the new required-metadata constraint.

`//agentplane/sampler/cmd:service_image` packages the service and pinned code-mode host.
The [k3s acceptance scenarios](../system_tests/README.md#central-sampler) run that image in its
own Pod, migrate before serving, and use service SVIDs over mTLS for
Gateway and History. The service and its shared JavaScript host use the Pod container's aggregate
CPU and memory limits.

Server-owned configuration names the service, provider connections and credential sources,
model contracts and per-model reasoning-effort allowlists, the rendered Agentplane
optional `platform_instructions` and exact service `service_callers` and `admin_service_callers`,
code-mode host binary, executor-side Codex path, logs, concurrency/deadlines and limits. Each named
connection uses `auth.type` (`bearer`, `agentplane_ingress` or `anthropic_api_key`). Bearer and
Anthropic connections require exactly one of `auth.credential` or `auth.credential_file`;
`agentplane_ingress` also accepts a central sampler service SVID in `tls`, with neither credential
field set. The Python manifest model validates the same authentication choices as the Go service.
The service reads credential files before each request so projected service-account tokens can
rotate. `AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL`
overrides `database_url` in the JSON settings, so database credentials can stay in a Secret-backed
environment variable. Gateway mode requires `gateway_target` and either a rotating projected
`gateway_token_file` or the sampler service SVID in `gateway_tls`. The sampler uses one generated `Executor` client for its Gateway and direct
local-test targets. It keeps one `OpenSession` gRPC stream to Runner and sends process and file
operations as typed unary RPCs. Runner owns the connection to Codex's exec-server; the gRPC stream
is not the Codex session itself. Closing the stream releases that adapter connection without
sending a process-termination request. Pinned Codex 0.171.0 keeps a detached native session for up
to 30 seconds, then expires it and terminates processes still running in it. The adapter adds no
durable session storage or new process policy. Unary request IDs correlate with the
sampler's durable intent journal; they do not deduplicate or cache server results. Configure
`executor_reconnect_timeout_seconds` (120 seconds by default) to bound attempts to resume a
retained native session during a Gateway outage, independently of the first-open and typed RPC
timeouts. An outage longer than the exec-server's detached-session lifetime can return a definitive
native loss response when Gateway recovers; the sampler then records a model notice before later work.

Optional `actions` settings contain `target` and a complete `tls` service-SVID source
(`cert_file`, `key_file`, `ca_file`, or `workload_api_socket`). The client requires the sampler's exact
service identity and verifies the Action Service SVID. There is no token, insecure mode or auth
fallback for this path. Omitted settings preserve the existing tool surface.

Before configuring this client, deploy and pin the Action Service image that accepts sampler
Session delegation, then enable its explicit sampler trust setting. Deploy the sampler image before
adding `actions` to its strict settings. Rollback removes client activation first, then receiver
trust, before reverting images. Retain Action receipts and the additive attribution schema.

Set `gateway_target` to `environments.{namespace}.svc:8082` for the Environments Runtime proxy.
Each Environments replica observes Runtimes independently, so the load-balanced Service works with
multiple replicas. Fixed targets support single-process fixtures. Templates accept DNS-label
executor names and only these two placeholders. A fixed
`direct_executor_target` supports standalone fixtures without the proxy only when
`direct_executor_insecure_localtest` is explicitly true and the target is a loopback IP address.
Choose exactly one proxy or direct execution target. Proxy service-SVID mode verifies the
receiver and sends no bearer. Optional `history_target` enables retained History export with either
`history_token_file` or the sampler service SVID in `history_tls`.

`max_live_sessions` caps concurrent active-execution slots, not resident Session drivers. A replica
retains the authoritative ownership lease, a lightweight driver and its heartbeat across idle turns
for each Session whose JavaScript state lives in its shared host. The driver waits without consuming
an active-execution slot. Protocol Session IDs and host generation remain stable across ordinary
idle turns. The host starts lazily when code-mode work first needs it and multiplexes Sessions,
while executor delegates and process-handle namespaces remain per Session. Interrupting a Session
does not stop the shared host.

Any replica may authenticate and durably admit a command. The transaction emits a payloadless
PostgreSQL notification on `central_sampler_work`; the shared wakeups Hub also listens on
`central_sampler_outbox` for History export. Work notifications carry no Session or Pod route:
resident owners scan pending work for their held lease token, while other replicas claim only
Sessions with unowned or expired leases. The runtime uses work notifications as wakeups for
level-triggered reconciliation: committed database rows remain authoritative. It subscribes before
its initial scan, then scans after listener reconnects and periodically, so missed or coalesced
notifications do not strand work.

During shutdown the service stops starting Session work and claiming unowned or expired Sessions,
wakes idle residents to release their leases, then joins drivers and completes ownership release
before stopping the shared host. The gRPC server can durably admit commands until graceful stop;
other replicas continue admitting throughout. Active effects retain the existing bounded drain and
uncertain outcome handling. The Executor adapter separately limits each Runner to eight open or opening
streams; that is a connection limit, not a limit on resident sampler Sessions or active execution.
See the [Executor API resource and lifecycle contract](../env/runtime/execution/SPEC.md).

Host failure interrupts pending work for every attached Session; later code-mode work may start a
fresh host, but in-flight provider or executor effects are never replayed automatically. Configure CPU
and memory limits on the sampler container. Those limits cover the Go service and shared host, so
container exhaustion can interrupt every active Session. PostgreSQL retains durable conversation
and effect records, but host-memory `store()` / `load()` values and live JavaScript cells are lost
when the shared host exits.

TODO: Evaluate a stronger kernel-enforced isolation boundary for cells or Sessions if aggregate
container limits stop meeting the production resource contract.

## Historical journal packing

The sampler image provides `compact-journal` for an online backfill of retained standalone rows.
It takes `--session ID` or `--all`, and requires an explicit `--dry-run` or `--apply`. Run it
after migration 21 and the rollout of block-capable readers to every sampler replica. The Job
image must also contain the maintenance command. Backfill works independently of the live
`event_block_writes` setting.

From the repo root, create a staging Job using the deployed image and the existing sampler
database Secret. This Bash function prints the Job name:

```bash
sampler_pack() (
  set -euo pipefail
  k=(kubectl -n agentplane-staging)
  image=$("${k[@]}" get deployment central-sampler \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="central-sampler")].image}')
  "${k[@]}" create --dry-run=client -f agentplane/sampler/ops/compact-journal-job.yaml -o json |
    jq --arg image "$image" --args '
      .spec.template.spec.containers[0] |=
        (.image = $image | .args = (["compact-journal"] + $ARGS.positional))
    ' -- "$@" |
    "${k[@]}" create -f - -o name
)
job=$(sampler_pack --all --dry-run)
kubectl -n agentplane-staging logs -f "$job" --pod-running-timeout=5m
kubectl -n agentplane-staging wait --for=condition=complete --timeout=1m "$job"
```

The per-Session summary counts the events and blocks selected, the existing stored frame bytes,
and the packed payload bytes. Indexes, row overhead and released filesystem space are separate
measurements. Preview a Session with `sampler_pack --session "$session_id" --dry-run`, then use
`--apply` on the same scope. Use `sampler_pack --all --apply` for all Sessions. The command
bounds each Session's pass by its starting cursor and leaves the incomplete tail for live writes.
Progress logs report the current cursor during long sessions. Apply pauses 50 ms between changed
windows. To stop it, delete the Job; a later run skips rows already moved into committed blocks.
Watch CNPG free space,
replication lag and WAL archival while it runs, especially if History maintenance shares the cluster.
See the [staging Job template](ops/compact-journal-job.yaml).

## Agentplane opt in

Staging selects the sampler service SVID for Gateway Executor and History export. History retains
the typed Session delegation envelope naming the execution Identity; the certificate proves the
sampler actor only. LLM ingress uses the same service SVID and typed Identity/Session delegation.
A selected SVID connection has no bearer fallback. Legacy outbound token settings remain in the
client API until their separate retirement.

- Gateway `central_sampler.tls` presents the Sessions role SVID and verifies the sampler server.
  The sampler's `gateway_tls` presents its own SVID and verifies the Gateway server for Executor calls.
  The Environments Runtime proxy's `environment_service.runtime_proxy.executor_callers` allowlist authorizes
  access to the same `Executor` API the Runner serves inside the Sandbox. Default empty settings preserve
  native operation and do not enable the new transport.
- The sampler exports to History with either its rotating caller token or `history_tls` configured
  with the History server URI, trust bundle, and central sampler client SVID. History explicitly
  grants the sampler service in its ingress policy; it retains the distinct executor
  Sandbox identity. With
  `ingress.sampler_service_actor`, History also admits the sampler pushing a Session's journal
  as a delegating actor for the Identity the Session executes as ([SPEC.md](SPEC.md#identity-and-admission)).
- The app's `HARNESS_CENTRAL_SAMPLER` offer maps to the `central_sampler_v1` placement and offers
  **Central sampler** alongside Codex and Claude. App and Actions callers do not supply service names
  or endpoints. A non-empty setup script runs once on the bound executor, in the resolved working directory,
  before sampling starts. Opening only reserves the Session and may run setup; the first model turn
  requires an explicit `SubmitInput`. Caller `additional_instructions` are an additive briefing
  mapped into `SessionSpec.instructions`, after server-owned platform guidance and the child
  Session ID. Its stdout, stderr and terminal outcome are retained in History.
- A central Session reaches LLM ingress only through a typed delegation envelope; ingress refuses a
  bare `x-agentplane-session-id` header
  ([attribution](../llm_ingress/README.md#trusted-central-session-attribution)). Ingress validates a
  canonical UUID and generates trusted metadata. Langfuse links filter by public Session ID; Sandbox
  identity describes execution.
  Responses `agentplane_ingress` connections use either the projected workload token in
  `credential_file` or `tls` configured with the LLM ingress server URI, trust bundle, and central
  sampler client SVID. The sampler samples a Session executing as an Identity as a delegating actor
  for it, which the ingress attributes the call to given `central_sampler_identity_callers`. The
  typed Identity and Session envelope is retained in either mode; SVID mode carries no Authorization
  header. `bearer` connections are never delegated to.

Readiness for the first open still requires a running executor. Binding is fixed to the Sandbox UID. After suspend/resume or replacement of its execution Pod,
initialization can adopt the authenticated new incarnation, abandon old execution handles, and
notify the model before continuing with a fresh execution session. Previous mutations are never replayed. A subsequent missing/suspended executor fails tool connection or execution;
central controls and persisted conversation remain on the sampler. Dispatcher commands still need
the extant bound Sandbox object; the standalone API can inspect/control its own persisted Session.

## Verification

Run `//agentplane/sampler/internal/residentruntime:unit_test` for the Session unit tests
without starting PostgreSQL. `//agentplane/sampler/internal/residentruntime:integration_test`
collects the scheduler and Session integration tests. Their individual targets are
`//agentplane/sampler/internal/residentruntime:scheduler_test` and
`//agentplane/sampler/internal/residentruntime/session:integration_test`. `//agentplane/sampler/internal/executor:executor_test` covers
facade settlement, cursor, image and model-visible no-replay behavior;
`//agentplane/sampler/internal/executor/transport:transport_test` covers Runner
session/resume, typed RPC and transport-level no-replay behavior;
`//agentplane/sampler/internal/sessionstore:sessionstore_test` covers Store, recovery,
outbox and migration adoption. RPC auth/status and request-streaming tests live in
`//agentplane/sampler/internal/rpc:unit_test` and `:integration_test`.
Leaf packages have colocated test targets; `bbr test //agentplane/sampler/...` includes them
in the Go and automated acceptance sweep. `//agentplane/history_service/internal/ingress:integration_test`
additionally checks real sampler export and History projection across recovery. Integration targets use real PostgreSQL,
pinned upstream host/executor processes, authenticated Gateway/History and scripted provider wire
fixtures. Scripted acceptance does not establish live provider or subscription compatibility.
Responses Lite compaction uses Codex's remote-v2 beta protocol. Anthropic uses native server-side
on-demand compaction; choose a supporting model. The manual
`//agentplane/sampler:test_live_anthropic_provider.live` target exercises real organization
API access, strict JSON exec, signed thinking and compaction across restart, and Haiku cache
creation/reads through an effort change. BuildBuddy must supply `ANTHROPIC_API_KEY` securely;
[persistent provisioning remains a follow-up](https://github.com/agentydragon/ducktape/issues/13063).
The automated cache test uses ordinary sampler instructions and tools without padding, and
asserts positive provider-reported cache counts plus stable system, tool and message prefixes.
A separate staging canary also verified reuse on unpadded inputs after an effort change.
The [Anthropic acceptance record](docs/anthropic_acceptance.md) describes the test scope and
observed rollout. The [sampler launch evidence](docs/initial_validation.md) records the prototype
and early Go tests, including the Pod and VM fixture failures.

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
      "auto_compact_tokens": 160000,
      "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
      "anthropic_messages": {
        "max_tokens": 16384,
        "thinking": { "type": "adaptive", "block_binding": { "prefix_mismatch_behavior": "error" } },
        "prompt_cache": { "ttl": "5m" },
        "cache_diagnostics": true,
        "per_message_effort": true,
        "strict_tool_input": true,
        "eager_input_streaming": true,
        "compaction": { "max_tokens": 4096, "keep_recent_turns": 1 }
      }
    }
  }
}
```

The Go implementation separates native protocol adapters into `providers/anthropic/`, `providers/responses/`
and `providers/chatcompletions/` packages. Each depends on `protocol/` for content, tool invocations,
context operations and the evidence sink; none imports the sampler or another API package. The sampler owns settings, credentials,
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

The adapter sends a stable system prompt and tool schema. Environment changes are appended durably.
Effort changes append an empty system control using Anthropic's beta, keeping
the launch effort fixed at the top level. Cache diagnostics compare against the last ordinary
provider response ID, including across sampler restarts. Cache counters and full native diagnostics
are retained in the DB; an unavailable diagnostic is not a cache miss.

`compaction` enables manual and sampler-triggered server-side on-demand compaction. It replaces only
active context, leaving the append-only conversation and History intact. `keep_recent_turns: 0`
summarizes all context; positive values preserve that many completed turns and any subsequent input.
Boundaries move backward over an effort control to keep signed thinking valid. The signed summary
goes first, followed directly by the unchanged suffix. Summarized effort is restored after the next
new user message. Compaction waits for model tool calls to be answered. Yielded cells keep running
on their existing host. The replacement context includes a snapshot of the handles at compaction
commit and any queued cell notifications. Failed compaction preserves old context. Background compaction and dynamic
definitions remain in the [task DAG](../plans/task_dag.md).
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
The paired tool result reports a cell's ID on yield and its outcome on settlement without adding
a separate inventory. From JavaScript, use the process `session_id` returned by `exec_command` to
continue with `write_stdin`.
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
A model change always preserves the API. An idle, settled ChatGPT Responses Lite Session can
switch between models on the same Agentplane ingress connection with the same tool encoding.
This includes model routes removed from configuration when retained provider requests establish
the matching contract. The sampler replays the active conversation window, including any opaque
compaction, and leaves the provider to handle incompatible reasoning.
If a Chat Completions route is removed, an idle, settled Session can switch to another Chat
Completions route and replay its retained messages and tool calls. Other switches require the
original route contract. See [the continuation rules](SPEC.md#model-changes). Standard routes
reject manual compaction at admission and automatic compaction settings at startup.
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

## Service certificate admission

`service_callers` and `admin_service_callers` grant exact service SPIFFE IDs access to their
respective RPC services and require `listen_tls.client_ca_file`. Both default empty, which admits
no caller. RPCs require a verified client SVID and do not accept bearer tokens or perform
account/Pod lookup.

Sampler Session ownership distinguishes service SPIFFE IDs from ServiceAccount UIDs. Migration 18
adds a separate service owner column without rewriting existing rows. A service cannot reopen or
read Sessions owned by an old account or another service. Before the Sessions caller switches to its
SVID, existing account-owned Sessions need an explicit ownership cutover; no certificate is implicitly
mapped to a Kubernetes UID.

The `central-sampler` image carries the ownership cutover as one-off commands, so no second image or
manual SQL is needed. Run one as a Job from the `central-sampler` Deployment's image, with
`AGENTPLANE_CENTRAL_SAMPLER_DATABASE_URL` taken from that Deployment's Secret (staging:
`postgres-central-sampler`, key `uri`). `--account-uid` is the legacy ServiceAccount's UID, not its name.

```text
central-sampler ownership inventory --account-uid <account-uid>
central-sampler ownership forward --account-uid <account-uid> --manifest <session-ids.json>
central-sampler ownership reverse --account-uid <account-uid> --manifest <session-ids.json>
```

- `inventory` prints one JSON array of the account's Session IDs, ordered by ID; `[]` means there is
  nothing to forward. Review the array and retain it as the manifest (the rollback scope), for example
  in a uniquely named ConfigMap mounted as the `--manifest` file.
- `forward` assigns exactly the manifest's Sessions to the Sessions service
  (`spiffe://agentplane/sessions`). Under a `sampler_session` table lock (other writers can wait
  briefly) it requires the manifest to equal the complete account-owned set and otherwise fails before
  changing anything. Repeating a committed `forward` is a no-op while the account owns no Sessions and
  every manifest Session is still service-owned. Afterwards `inventory` for the same UID must print
  `[]`: a listed ID was created after the lock was released and is outside the manifest.
- `reverse` is the rollback. It validates every listed owner, restores only the manifest's Sessions to
  the account, and is safe to repeat. Sessions created under the SVID after the switch are outside the
  manifest and need their own reviewed manifest before a bearer-only caller is restored.

Both rewrites change only `account_uid` and `service_spiffe_id`; they never delete Sessions or
conversation data.

Keep the Job's Pod outside the sampler's selectors: the Service matches the Deployment's
`cdk8s.io/metadata.addr`, while the PDB and the sampler's `CiliumNetworkPolicy` match
`app.kubernetes.io/name: central-sampler`. Label it `app.kubernetes.io/name: sampler-owner-maintenance`
with a unique `app.kubernetes.io/instance: sampler-owner-<run-id>`, and give it a run-scoped
`CiliumNetworkPolicy` that allows only cluster DNS and the CNPG `postgres` cluster's port 5432.
