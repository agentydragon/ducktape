# Sampling outside Sandboxes

**Research, no direction chosen (operator request, 2026-10-27).** Could Agentplane run the agent loop
(model sampling, prompt assembly, the native conversation store) outside Sandboxes, with a Sandbox only
running the agent's processes and file operations? Rai: "could we run LLM sampling and talking to ai
APIs outside of the sandboxes, just executing processes inside them? Iiuc, codex code mode has synergy
with that direction. And if we have a mode where we run sampling in agentplane outside sandboxes, that
would allow us to e.g. better control things like memory retrieval."

Codex evidence is read from [`openai/codex` at `rust-v0.162.1`][tag] (commit `092d3acd`), the
`agentplane_codex_cli_linux_x64` pin. Claude Code evidence comes from strings in the runner-pinned
2.1.252 binary. Nothing here was run.

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

### Reaching a Sandbox's exec-server

The app-server dials each exec-server over WebSocket; a bearer needs `wss://` or loopback. The other
direction, the exec-server registering itself, either uses a Noise relay to OpenAI's environment
registry or, in "direct" mode, requires AWS SigV4 ([exec-server README][es-readme]). Neither fits
Agentplane, whose runners dial out and admit no connections ([runner control](../docs/runner_control.md)).
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

**Codex does not load AGENTS.md from where the agent reads, writes or runs.** It loads
`AGENTS.override.md` or `AGENTS.md` (or a configured fallback name) in every directory from the project
root (the nearest `.git`, or configured markers) down to the turn environment's cwd, concatenated in
that order ([agents_md.rs][agents-doc]), through that environment's filesystem, so a central harness
still reads the Sandbox's files. Before every sampling step it refreshes, but re-reads the repository
only when the environment selection (ids, cwds, roots) or project trust changed
([agents_md_manager.rs][agents-cache], [session/mod.rs][agents-step]); a changed result is sent as a new
fragment that replaces all previous AGENTS.md instructions ([world_state/agents_md.rs][agents-diff]). A
`cd` inside a command, the paths of an `apply_patch` or a file a cell reads leave the selection alone;
only a `turn/start` cwd or environment override changes it. `agents_md.rs` is the only AGENTS.md loader
in `codex-rs/core`.

Nested files are left to the model. Codex's default base instructions and the GPT-5.1 and 5.2 prompts
say "When working in a subdirectory of CWD, or a directory outside the CWD, check for any AGENTS.md
files that may be applicable" ([default.md][agents-spec]); no entry in the tag's `models.json`, GPT-6
included, carries that passage. Code mode changes nothing here: a cell neither moves the turn's cwd nor triggers a load.

**Claude Code does.** At start it loads CLAUDE.md from the cwd upward
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

## Our own harness instead of Codex

Rai: "Or generally any things that codex does that would be hard to replicate if we run it or an
analogous harness we write ourselves outside the sandbox where its executed commands run". Run outside,
Codex itself loses only the host-side items [above](#codex-supports-the-split-upstream). A harness
Agentplane writes would have to reproduce, beyond where it runs:

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

Every control point above (environment placement, per-turn context, driver tools, hooks, thread
instructions, per-thread configuration) is already in Codex's client API, so running upstream Codex
centrally gets them without that cost. A harness of our own earns its cost only for a control need that
API cannot meet. Rewriting requests at the LLM ingress is a third route, but it fights the harness's
world-state diffs and prompt caching.

## Claude

Rai, 2026-10-27: "anthropic wants you to run the Claude CLI when you're on a subscription while openai
seems more chill about you talking to codex's api". On the subscription the CLI stays the client, so
this direction is Codex first.

Claude Code 2.1.252 has no usable remote tool backend: the bundle reads
`CLAUDE_CODE_REMOTE_TOOLS_{SERVE,POLICY,SESSION_CHANNEL}`, but the availability check beside them is
compiled to `false` in the public build. A central Claude would run the CLI with `--tools ""` and driver
tools ([driver tools](../docs/driver_tools.md)) that act in the Sandbox. That gives up the built-in
tools' behavior: nested CLAUDE.md on Read, read-before-edit tracking, background shells, image and PDF
reads, and the tool names the model knows. Agentplane would reimplement nested CLAUDE.md in its own read
tool. Claude stays in the Sandbox unless a separate case is made.

## Next

`CENTRAL_CODEX_SPIKE`, then `HARNESS_PLACEMENT_DESIGN`, in [task DAG](task_dag.md) section 5.

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
[agents-cache]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agents_md_manager.rs#L86-L93
[agents-step]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/session/mod.rs#L3766-L3775
[agents-diff]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/context/world_state/agents_md.rs#L11-L13
[agents-spec]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/protocol/src/prompts/base_instructions/default.md#L17-L27
[hook-ctx]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/hooks/src/events/post_tool_use.rs#L43-L50
[add-ctx]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L207-L210
[add-ctx-cap]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/context-fragments/src/additional_context.rs#L6
[mem-readme]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/memories/README.md#phase-2-global-consolidation
