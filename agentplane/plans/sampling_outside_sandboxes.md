# Sampling outside Sandboxes

**Research, no direction chosen (operator request, 2026-10-27).** Could Agentplane run the agent loop
(model sampling, prompt assembly, the native conversation store) outside Sandboxes, with a Sandbox only
running the agent's processes and file operations? Rai: "could we run LLM sampling and talking to ai
APIs outside of the sandboxes, just executing processes inside them? Iiuc, codex code mode has synergy
with that direction. And if we have a mode where we run sampling in agentplane outside sandboxes, that
would allow us to e.g. better control things like memory retrieval."

Codex evidence is read from [`openai/codex` at `rust-v0.162.1`][tag] (commit `092d3acd`), the
`agentplane_codex_cli_linux_x64` scripted-test pin. Claude evidence includes static inspection of
the 2.1.252 test pin and this session's installed 2.1.296, plus existing scripted-test source
([versioned evidence](../debug/claude_execution_placement.md)). The runner image uses independently
pinned nixpkgs harnesses. No new inference or execution-placement probe was run for this research.

## Today

- The Runner runs one Codex app-server for all its Codex Sessions, with `CODEX_HOME` at
  `<state_dir>/codex` on the Sandbox's state volume, and one Claude Code process per Session
  ([harness process design](../docs/runner_delivery_and_harness_design.md#harness-process-store-and-session-identity)).
  Rollouts, compaction output and harness configuration are files the agent's own tools can read and
  rewrite: the [isolated executor](../runner/docs/codex_executor.md) is a resource boundary, not a
  trust boundary.
- Sampling already leaves the Sandbox on the wire. The harness calls the
  [LLM ingress](../llm_ingress/README.md) with a placeholder bearer that the egress proxy substitutes;
  the ingress passes bodies through and attributes the request to the calling Pod.
- Every process in a Sandbox can sample. The `basic` egress policy grants the whole LLM ingress with the
  `agentplane-workload` credential (`cluster/cdk8s/agentplane/egress.py`), and the placeholder is
  published in runner instructions ([egress](../egress/README.md#rules-api)).
- Isolated executor mode already splits Codex in two inside the Sandbox: the app-server in the runner
  container and `codex exec-server` in a companion container, joined by `CODEX_EXEC_SERVER_URL`.

## Codex supports the split upstream

An app-server holds any number of execution environments and binds them per thread and per turn:

- `environment/add` registers an exec-server at runtime: `environmentId`, `execServerUrl`, an optional
  bearer, timeout and required skills ([environment.rs][env-add]). `thread/start.environments` sets a
  thread's sticky environments (id, cwd, runtime workspace roots); `turn/start.environments` overrides
  them for that turn and later ones ([thread.rs][thread-env], [turn.rs][turn-env]). All three are
  experimental API.
- With `CODEX_EXEC_SERVER_URL` set, as the Runner sets it in isolated mode, the local environment is
  not offered at all ([environment_provider.rs][no-local]).
- Sub-agents inherit their parent's environments ([spawn.rs][spawn-env]).

What goes to the selected environment:

| Operation                                     | Where it runs                                                                                                            | Source                                                 |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------ |
| `exec_command` / `write_stdin` (unified exec) | exec-server `process/*`; the executor applies `shell_environment_policy`, so a thread's `set` map still reaches commands | [unified_exec.rs][ue-remote], [env policy][env-policy] |
| `apply_patch`                                 | environment filesystem                                                                                                   | [apply_patch.rs][patch-fs]                             |
| `view_image`                                  | environment filesystem                                                                                                   | [view_image.rs][image-fs]                              |
| AGENTS.md discovery                           | environment filesystem                                                                                                   | [agents_md.rs][agents-fs]                              |
| stdio MCP servers                             | the server's `environment_id`, default `local`                                                                           | [mcp_types.rs][mcp-env]                                |

What stays on the harness host:

- the model client, rollouts and the state database under `CODEX_HOME`;
- code-mode JavaScript ([below](#code-mode));
- command hooks, spawned with the app-server's own environment ([registry.rs][hooks-env],
  [command_runner.rs][hooks-spawn]);
- Codex's own memories under `CODEX_HOME/memories` ([extension.rs][mem-home]);
- per-turn git metadata for request headers, which runs `git` in the host's copy of the cwd
  ([turn_metadata.rs][git-meta]) and finds nothing for a remote one;
- the network credential broker, which unified exec turns off for remote environments
  ([unified_exec.rs][ue-remote]).

### First runnable slice

Worker `/root/central_codex_spike`, 2026-10-27: the experimental
[`runner/x/central_codex`](../runner/x/central_codex/README.md) subprocess/relay fixture passed on RBE
in 16.3 seconds ([evidence](../debug/central_codex_sampling.md)). It drives real code-mode shell, patch,
image and remote instruction loading, keeps native state outside the executor directory, and reads
conversation history after executor loss and sampler restart. The host explicitly records unknown
effects and interrupts a disconnected turn: a yielded shell session is not terminal failure evidence.
The separate Pod/VM failure domain, real fence, authenticated Gateway path, spend attribution and
comparable nested-call latency remain open. No deployment changes.

### Reaching a Sandbox's exec-server

The app-server dials each exec-server over WebSocket; a bearer needs `wss://` or loopback. The other
direction, the exec-server registering itself, either uses a Noise relay to OpenAI's environment
registry or, in "direct" mode, requires AWS SigV4 ([exec-server README][es-readme]). Neither fits
Agentplane, whose runners expose private mTLS gRPC servers ([runner control](../docs/runner_control.md)).
What fits is a byte tunnel: the Runner Gateway serves a per-Sandbox WebSocket endpoint, carries it over
the runner's existing outbound connection to the loopback exec-server, and the central harness
`environment/add`s that endpoint with a per-Sandbox bearer.

## Code mode

Current GPT-6 catalog entries are `tool_mode: code_mode_only` with `use_responses_lite` (the tag's
`models-manager/models.json`; GPT-6 Luna's bundled entry is what `runner/test_codex_code_mode.py` runs).
The model sees only `exec` and `wait` and writes a JavaScript cell that calls `tools.exec_command`,
`tools.apply_patch` and the other tools. The cell runs in a code-mode host: by default a separate
`codex-code-mode-host` child of the app-server (`code_mode_host` is stable and on,
[features][cm-feature], [thread_manager.rs][cm-local]), or a remote host over gRPC
([app-server lib.rs][cm-grpc]). Each nested `tools.*` call goes back to the app-server's tool router
([delegate.rs][cm-delegate]) and from there to the turn's environment.

So with the harness outside the Sandbox, a cell's control flow (loops, parsing output, choosing the
next command) runs outside it too, and only leaf process and file calls cross in:

- one sample drives many Sandbox operations, so moving sampling out costs fewer model round trips
  than a function-calling loop would;
- every effect on the Sandbox is a typed exec-server RPC (`process/start`, `fs/writeFile`, …) the
  tunnel can meter and audit;
- tools the driver serves (`thread/start.dynamicTools`, [driver tools](../docs/driver_tools.md)) sit in
  the registry cells call into; the `code_mode_only_strict_third_party_tools` feature describes them as
  deferred "in exec" ([features][cm-dyn]). A cell could call an Agentplane tool, a memory search for
  example, without a model round trip and without the Sandbox seeing it. Not yet tested.

Moving the harness does not close `CODEX_CODE_MODE_CELLS`: the app-server still has no item for a cell.
Each nested call becomes a cluster round trip instead of a loopback one.

## AGENTS.md: where the agent operates

**Codex does not load AGENTS.md from where the agent reads, writes or runs.** It sends one user message,
"# AGENTS.md instructions for `<cwd>`" with the text in `<INSTRUCTIONS>` ([user_instructions.rs][agents-msg]),
built from two sources ([agents_md.rs][agents-doc]):

- `$CODEX_HOME/AGENTS.override.md` or `AGENTS.md`, read from the harness host's own filesystem
  ([instructions/mod.rs][agents-global]). The Runner writes neither; Session instructions go in
  `developerInstructions`.
- After a `--- project-doc ---` separator, the project chain: `AGENTS.override.md`, else `AGENTS.md`, else a
  `project_doc_fallback_filenames` name, in every directory from the project root (the nearest `.git`, or
  `project_root_markers`) down to the turn's cwd, root first. It is read through the turn environment's
  filesystem ([agents_md.rs][agents-fs]). For a remote exec-server that is its `fs/getMetadata` and
  `fs/readFile` RPCs, and the cwd and the root search are paths on the exec-server's machine, so a central
  harness still reads the Sandbox's files. Each environment of a multi-environment turn adds its own
  chain, labelled "for `<environment>` with root `<cwd>`" ([agents_md.rs][agents-label]). All chains share
  one `project_doc_max_bytes` budget, 32 KiB by default: the file that crosses it is truncated and later
  files are dropped ([agents_md.rs][agents-cap]). A project whose `projects.<path>.trust_level` is
  `untrusted` gets no chain ([agents_md.rs][agents-trust]); Agentplane sets no trust level.

**A turn's cwd** is a thread setting the harness stores, not a shell's working directory. It is the
default `workdir` of `exec_command`, where the project chain ends, and what the model is told it works in.
Only the app-server's client changes it: `cwd` on `thread/start`, `thread/resume` or `thread/fork`
([thread.rs][cwd-thread]), `cwd` or a per-environment cwd on `turn/start`, for that turn and later ones
([turn.rs][cwd-turn]), and `thread/settings/update` ([thread.rs][cwd-settings]). A `cd` inside a command,
the paths of an `apply_patch` or a file a cell reads leave it alone. The Runner sends the Session spec's
`cwd` once, on `thread/start` (`runner/codex/adapter.go`), and a Session's spec never changes
([runner SPEC](../runner/SPEC.md)), so each Session has one cwd and one chain for its whole life.

Before every sampling step Codex re-reads the global file, but re-reads the project chain only when the
environment selection (ids, cwds, roots) or project trust changed ([agents_md_manager.rs][agents-cache],
[session/mod.rs][agents-step]), so a project AGENTS.md edited mid-thread is not picked up. A changed result
is sent as a new fragment that replaces all previous AGENTS.md instructions
([world_state/agents_md.rs][agents-diff]). `agents_md.rs` is the only AGENTS.md loader in `codex-rs/core`.

Two other Codex features read these files. Auto-review counts AGENTS.md as trusted
content that can establish user authorization ([guardian policy][agents-guardian]), and the `/review`
rubric applies the instruction files scoping each changed file ([review rubric][agents-review]). Since the
project chain comes from the Sandbox wherever the harness runs, an agent that can write its repository
can write instructions auto-review would trust; a central harness does not change that.

Nested files are left to the model. Codex's default base instructions and the GPT-5.1 and 5.2 prompts
say "When working in a subdirectory of CWD, or a directory outside the CWD, check for any AGENTS.md
files that may be applicable" ([default.md][agents-spec]); no entry in the tag's `models.json`, GPT-6
included, carries that passage. Code mode changes nothing here: a cell neither moves the turn's cwd nor triggers a load.

**Claude Code does load by where the agent reads.** At start it loads CLAUDE.md from the cwd upward
([docs](https://code.claude.com/docs/en/memory)). In 2.1.252, a file the Read tool reads (text, image
or notebook), a file the user @-mentions, or a file opened in the IDE becomes a nested-memory trigger.
Before the next model request Claude Code attaches every CLAUDE.md and path-scoped rule between the cwd
and that file that it has not yet loaded, as a `nested_memory` attachment rendered "Contents of
`<path>`:", once per file per conversation. Bash, Edit and Write do not trigger it. The CLI reads those
files with its own filesystem calls.

Agentplane could give Codex the Claude behavior without changing harnesses. A Codex `PostToolUse` hook
can return context for the model ([post_tool_use.rs][hook-ctx]), so a hook seeing `apply_patch` paths or
an `exec_command` working directory could attach the nested AGENTS.md files not yet attached. Hooks run
on the harness host, so with a central harness the hook would read those files through the same
tunnel. Whether `PostToolUse` fires for calls nested in a cell is unverified.

## What it would enable

- **No sampling authority in the Sandbox.** A Sandbox on a central harness needs no LLM ingress grant,
  so agent processes can neither spend nor reach models; an agent program that wants a model gets an
  explicit grant.
- **Conversation state out of the agent's reach.** Rollouts, compaction summaries, instructions and
  hook configuration live where the agent's tools cannot read or rewrite them.
- **Memory retrieval Agentplane controls.** Agentplane chooses what to retrieve and when, and neither the
  store nor the retrieved text enters the Sandbox: per turn through `turn/start.additionalContext`
  (keyed entries of at most 1000 tokens, kind `untrusted` or `application`; [turn.rs][add-ctx],
  [additional_context.rs][add-ctx-cap]), mid-turn through `turn/steer.additionalContext`, on demand
  through driver tools (callable from cells), and through `thread/start.developerInstructions`. The
  Runner is the app-server's client wherever the harness runs, so this API works from inside the
  Sandbox too; placement decides whether the store and retrieved text sit inside the agent's trust
  domain, and whether an agent process can go around retrieval by calling the ingress itself.
- **Policy outside the agent's reach.** Command hooks and approvals run on the harness host.

Codex's own memories do not fit a shared harness: they live in one `CODEX_HOME`, and Phase 2
consolidates every eligible thread's memories into one workspace ([memories README][mem-readme]). A
shared app-server would have to turn them off, or each owner gets an app-server of their own.

## What it costs

- **A home for the harness.** Inside the Sandbox, the Runner owns harness supervision, the journal,
  recovery and the push to History. A central harness needs those in the control plane: either a
  harness Pod per Sandbox (today's failure scope, nothing shared between owners) or a shared
  app-server, where one exit fails every hosted Session's turn and owners share `CODEX_HOME`,
  configuration and memories. The journal could stay with an in-Sandbox Runner that remains the
  app-server's client through a tunnel, but then conversation content transits the Sandbox again.
  Either way this meets the frozen `RUNNER_STATE_BOUNDARY_RETHINK` and `RUNNER_CENTRAL_ADMISSION`
  ([freezer](task_freezer.md)).
- **The exec-server tunnel**: its per-Sandbox bearer, interrupts that cross it, and what a lost tunnel
  means. An exec-server restart already loses running processes, and Codex does not replay them
  ([executor](../runner/docs/codex_executor.md#scope-in-codex-01621)).
- **Latency.** Every nested call and filesystem probe is a cluster round trip. AGENTS.md discovery
  probes ancestors concurrently (up to 256 in flight), about one round trip.
- **Spend attribution.** The ingress attributes a request to the calling Pod, which would be the
  harness. A per-thread model provider carrying a per-Session token
  (`model_providers.*.http_headers` or `experimental_bearer_token` through `thread/start.config`) might
  work in a shared app-server; unverified. A harness Pod per Sandbox keeps Pod attribution.
- **Host-side features to place.** stdio MCP servers need an explicit `environment_id`; command hooks
  move to the control plane (wanted); per-turn git metadata is lost (cosmetic); Codex memories as
  above.
- **Modes coexist.** Sandbox-hosted Codex with local tools, Sandbox-hosted Codex with the isolated
  executor, and central Codex with the Sandbox's exec-server can run side by side; a new mode applies
  to new Sandboxes, and none retires the others.

## A Postgres-backed Codex fork or custom harness

Rai: "Or generally any things that codex does that would be hard to replicate if we run it or an
analogous harness we write ourselves outside the sandbox where its executed commands run". Run outside,
Codex has the host-side placement limits [above](#codex-supports-the-split-upstream). A custom
harness needs to account for each feature it retains, beyond where it runs:

- **The model's tool contract.** GPT-6 entries are code mode only on Responses Lite; the model expects
  `exec` and `wait`, the `tools.*` API, `apply_patch`'s grammar, unified exec's yield and stdin
  semantics, and the catalog's `model_messages`. Departing from what the model was trained against costs
  quality.
- **The code-mode runtime**: the V8 host; cell yield, wait and interrupt; limits; the nested-call broker
  ([delegate.rs][cm-delegate]).
- **Context management**: instructions as world-state sections re-sent as diffs, automatic and
  provider-side compaction, token budgets, encrypted reasoning items, cache-stable request order, the
  Responses WebSocket transport.
- **Multi-agent v2, auto-review, skills and plugins, the MCP client with OAuth, hooks, approvals and
  exec policy.**
- **Rollouts, resume, fork and `thread/items/list`**, which the Runner's recovery reconciliation reads
  ([runner](../runner/README.md#native-continuation-evidence)).

Central placement and persistence ownership are independent choices. Upstream Codex centrally
retains its native store plus Agentplane's command/Event journal. A fork or custom sampler could
make Postgres authoritative for the model conversation, prompt/context state, turns, tool intents,
results and recovery checkpoints. This could remove several layers whose purpose is observing a
remote, independently persistent harness. Removing CLI rendering alone does not measure that saving:
the current headless app-server already avoids an interactive terminal UI.

| Current responsibility                                                    | Candidate in a central Postgres-backed mode                                                                                                  |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Native rollout files and state DB, plus Runner SQLite journal             | One service-owned durable conversation store; preserve the native/provider facts needed for continuation                                     |
| Runner journal push, exact-replay acknowledgements and seal-before-delete | Central commits protect conversation state independently of executor storage; executor effects still need receipts and fencing               |
| Native transcript versus Runner-event recovery reconciliation             | Recover the sampler from committed state; retain uncertainty about unobserved model responses and tool effects                               |
| History raw archive and derived Thread fold                               | Keep retained history and frontend projections; evaluate serving them from the same authoritative log without a second ingestion copy        |
| Dispatcher command delivery and Runner Gateway proxy                      | Central input admission may become local to the sampler; retain authenticated executor dial-out, routing, cancellation and lifecycle control |

These are proposed responsibility changes, not permission for multiple services to write each
other's tables or a decision to delete services. The [current authorities](../docs/thread_layering.md#authorities-and-representations),
[Gateway](../runner_gateway/README.md) and [History](../history_service/README.md) still provide those
contracts. Central sampling alone does not replace them. Any extraction must preserve retained
Sessions, identities, history and native state under the staging data-preservation procedure.

### Bounded spike: measure the headless subset we need

Operator, 2026-10-27: investigate whether our own "Codex" is small enough to justify tighter tracking
of OpenAI APIs. **Baseline merged in
[PR #12207](https://github.com/agentydragon/ducktape/pull/12207), `/root/postgres_harness_spike`,
2026-10-27.** The operator-authorized code-mode-only implementation is isolated under
`agentplane/x/postgres_codex`. The [runnable experiment and measured findings](../debug/postgres_codex_spike.md)
record 563 physical Python runtime lines, a separate upstream source/dependency inventory, and seven
passing RBE cases. It reuses real pinned upstream code-mode host, exec-server and apply-patch binaries.
A persistence-focused Codex fork was not implemented or measured.

The scripted model turn exercises remote shell and patch, then continues in a fresh sampler process
from Postgres, without a native rollout or second Runner conversation journal. The database retains
actual provider requests/responses, including opaque encrypted reasoning, and correlated tool intents
and results. An independent full upstream Codex consumer also accepts the scripted wire. This does
not establish live provider or subscription compatibility. The recommendation favors central upstream
Codex for broad compatibility today; the custom loop is a candidate if its bounded completed-cell
checkpoints and tool contract suffice.

Crash checks distinguish committed intent, dispatch, effect and returned result. Database-clock leases
and ownership generations fence stale sampler writes. Postgres cannot atomically commit a shell side
effect: a real append followed by a lost reply remains uncertain and is not automatically replayed.
Sampler and executor SIGKILL cases preserve readable conversation state and bound failure reporting
([incident #12203](https://github.com/agentydragon/ducktape/issues/12203)). A dispatch can race after
committed intent; database fencing does not guarantee exactly-once remote effects. There is no
reconciliation interface yet.

The [process-control extension](../debug/postgres_codex_spike.md#process-control-extension),
in [PR #12213](https://github.com/agentydragon/ducktape/pull/12213), adds real yielded-cell `wait`, durable shell session handles,
interactive stdin, PTY EOF/Ctrl-C and external generation-targeted interruption. A retained host is
required for live V8 state; process continuation uses the upstream executor session's 30-second
detached retention window. These are explicit limits, not Postgres serialization of live processes.
Compaction is a separate extension. Instruction discovery, persistent JavaScript storage, subagents,
skills/MCP, approvals, hooks, memory, History export and frontend tracing remain omitted. A frontend
trace was not an acceptance gate: the operator clarified that existing Langfuse links already provide
partial visibility. Neither extension establishes production replacement or deployment.

### Model exchange visibility

A custom central sampler owns the request/response boundary before native adapters translate it.
Merely relocating upstream Codex does not expose every model payload through app-server; equivalent
evidence would need model-client instrumentation or an ingress capture path. Record a model-exchange ID linked to Session, turn and tool IDs, with ordered streaming parts,
completion/error, usage and timing. Keep the request actually sent distinct from assembled input,
frontend projections, and a recovered or retried attempt. This lets the frontend show prompts,
instruction/context sources, compaction and tool results without inferring the model exchange from
SDK events. Preserve opaque provider continuation data needed for resume. Store model
payloads without substituted credentials under conversation authorization and an explicit retention policy; do not put auth headers
or credential-bearing URLs in frontend traces. Central ownership must still bound buffering and apply
backpressure when storage or a viewer is slow. The spike should show what fidelity upstream's API
already exposes and what a fork/custom loop gains.

## Claude

[Collected evidence](../debug/claude_execution_placement.md) distinguishes built-in local tools,
driver-hosted MCP, native remote-tool serving, Remote Control/RemoteIO and provider wire cloaking.
Subscription use alone does not establish that the CLI must execute tools locally. Technical wire compatibility, live provider acceptance and supported account use
are separate questions.

The 2.1.252 test build compiles the remote-tool entry gate off. Installed 2.1.296 instead has a local
`CLAUDE_CODE_REMOTE_TOOLS_FORWARD` switch plus account flags and bridge requirements. Its native
serving path uses per-caller read state and the tool pipeline, but full nested `CLAUDE.md` propagation
to the remote model request is unverified. Enabling the local predicate does not prove a self-hostable
backend. A shell proxy alone leaves filesystem tools, instruction loading and related state local.

Alternatives to compare:

- **Codex-only central mode.** Use its upstream execution-environment API; Claude remains local.
  Claude investigation does not block that mode.
- **Claude native remote tools.** Characterize the newer binary's gate, transport and built-in
  semantics with a scripted peer before committing to integration.
- **Central Claude CLI plus custom tools.** Disable built-ins with `--tools ""`, supply remote tools
  through the tested driver MCP seam, and explicitly reproduce wanted behavior such as nested
  instructions and read-before-edit state. Existing MCP tests do not establish full parity.
- **Custom sampler through CLIProxyAPI.** Send model API requests using its translation/cloaking or
  a required CLI/SDK-shaped identity. The pinned proxy implements those mechanisms, but no live
  compatibility was tested here. Agentplane owns the tool/context loop and persistence; presenting
  a Claude identity does not supply Claude's built-in tool behavior.

## Accepted central sampler v1

Operator agreement, 2026-10-27: implement and open a PR for an independent central code-mode
sampler with its own database and standalone API. The shared sampler hosts a separately supervised,
resource-limited JS process per live Session. Dispatcher admits/routes central commands directly
to it; tools use sampler → Gateway → outbound Runner tunnel → loopback exec-server. Wrapped Codex
and Claude keep execution and sampling together on their existing paths. Experimental code lives
under `x/`; no deployment, default change or native-state migration is selected.

Operator request, 2026-10-28: rewrite the sampler in Go. This includes the service, provider/tool
loop, persistence and History exporter. Preserve the prototype's API, PostgreSQL state,
persistence/failure semantics and acceptance scenarios. The upstream JS host remains a separately
supervised process. Opt-in deployment is a separate DAG outcome.

The selected provider scope is OpenAI-shaped Responses/code mode with pinned Codex 0.162.1 host
and executor. Compaction, wait, stdin and interrupts are required. Standing instructions are
supported; remote AGENTS.md discovery is useful but deferred. Executor replacement is deferred:
new Sessions bind one Sandbox UID/incarnation. See the [API](../x/central_sampler/sampler.proto),
[service contract](../x/central_sampler/SPEC.md) and active nodes in [the DAG](task_dag.md).

The sampler owns continuation not represented in History. It exports critical reasoning, text,
tool and lifecycle Events plus underlying evidence through existing RunnerIngress. Public Session
ID is the inference identity, with Sandbox/incarnation identifying the executor. Central requests
can carry a trusted Session metadata claim into Langfuse. Request/response evidence and normalized
frontend projection are distinct; Thread remains a lossy view for display.

### Coexistence and history across backends

Keep wrapping harnesses we do not own as an independently usable backend, including official
Claude and native Codex. The operator selected this transitional capability independently of the
custom loop; external hosted agents and MCP clients similarly remain useful edges. No custom
sampler success is a reason to retire wrapped harnesses or silently migrate their state.

Provider APIs and harness JSON interfaces expose different facts. A common frontend needs a common
projection, but native Codex does not expose everything a model did inside JS cells. Preserve raw
backend-specific evidence and opaque continuation without inventing absent details. Provider context,
human-visible history and execution-effect evidence serve different purposes: compaction can change
context without erasing history. Source-linked normalized Events extend the existing projection;
they do not claim a universal lossless transcript. Broader provider/history design remains separate.

The gated Claude native remote-tools investigation and custom Claude sampler experiment remain
independent branches. A later Claude sampler could share this code-mode runtime after prompting and
contract characterization. CLIProxyAPI translation/cloaking alone does not supply native tool,
instruction, cache or continuation semantics or prove live provider/account support.

[tag]: https://github.com/openai/codex/tree/rust-v0.162.1/codex-rs
[env-add]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/environment.rs#L38-L53
[thread-env]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L136-L144
[turn-env]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L211-L218
[no-local]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/src/environment_provider.rs#L92-L104
[spawn-env]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agent/control/spawn.rs#L479-L500
[ue-remote]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/runtimes/unified_exec.rs#L293-L303
[env-policy]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/unified_exec/process_manager.rs#L194-L219
[patch-fs]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/handlers/apply_patch.rs#L339
[image-fs]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/handlers/view_image.rs#L158
[agents-fs]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agents_md.rs#L58-L82
[mcp-env]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/config/src/mcp_types.rs#L24
[hooks-env]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/registry.rs#L79
[hooks-spawn]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/engine/command_runner.rs#L26
[mem-home]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/ext/memories/src/extension.rs#L45-L53
[git-meta]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/turn_metadata.rs#L573-L585
[es-readme]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec-server/README.md#remote-connections
[cm-feature]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/features/src/lib.rs#L1140-L1145
[cm-local]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/thread_manager.rs#L570-L577
[cm-grpc]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server/src/lib.rs#L602-L618
[cm-delegate]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/code_mode/delegate.rs#L36-L42
[cm-dyn]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/features/src/lib.rs#L142-L144
[agents-doc]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agents_md.rs#L1-L18
[agents-msg]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/context/user_instructions.rs#L23-L34
[agents-global]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/codex-home/src/instructions/mod.rs#L40-L60
[agents-label]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agents_md.rs#L445-L455
[agents-cap]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agents_md.rs#L150-L166
[agents-trust]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agents_md.rs#L64-L66
[cwd-thread]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L81
[cwd-turn]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L219-L221
[cwd-settings]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L242-L244
[agents-guardian]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/prompts/templates/guardian/policy_template.md#L6
[agents-review]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/prompts/templates/review/rubric.md#L48
[agents-cache]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agents_md_manager.rs#L86-L93
[agents-step]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/session/mod.rs#L3766-L3775
[agents-diff]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/context/world_state/agents_md.rs#L11-L13
[agents-spec]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/protocol/src/prompts/base_instructions/default.md#L17-L27
[hook-ctx]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/events/post_tool_use.rs#L43-L50
[add-ctx]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L207-L210
[add-ctx-cap]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/context-fragments/src/additional_context.rs#L6
[mem-readme]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/memories/README.md#phase-2-global-consolidation
