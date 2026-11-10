# Claude execution placement evidence

Investigation, 2026-10-27 PDT. Motivation: an agent's file operations preceded the loss of
`virtiofs-state` on `public-coder-ducktape-1`
([incident #12203](https://github.com/agentydragon/ducktape/issues/12203)). Keeping sampling,
conversation persistence and recovery outside the execution VM would let the conversation survive
that failure. This does not repair the state filesystem or make an interrupted tool safe to replay.

## Evidence boundary

Static inspection and existing scripted-test source; no new inference, account feature changes or
bridge registration performed. Existing tests below are evidence of their asserted scenarios, not a
new test run. Versions differ:

| Artifact            | Provenance                                                                               | What was inspected                                                                                                              |
| ------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code 2.1.252 | [`MODULE.bazel`](../../MODULE.bazel), `claude_code_cli_linux_x64`                        | npm tarball downloaded; SHA-256 matches `ecce38cb26f10215a98608c23ddaf4db6fe07bce651c0367617f8829569824fb`; embedded JavaScript |
| Claude Code 2.1.296 | This investigation's `/bin/claude --version`                                             | Nix `.claude-wrapped` binary; SHA-256 `8ae9374664a714e8c56d18c6ed22aa024325e7d4b753ff0a69727a2571425537`; embedded JavaScript   |
| CLIProxyAPI         | [`third_party/cli_proxy_api/MODULE.bazel`](../../third_party/cli_proxy_api/MODULE.bazel) | Fork source commit `0f5c54cd6a1393447484eee02149bf7fab2e4784`                                                                   |

The [runner image](../env/images/image.nix) uses nixpkgs harnesses, independently of the scripted-test
pins. Neither the 2.1.252 baseline nor this session's installed 2.1.296 establishes which binary ran
in the incident VM.

## Local built-ins and the driver seam

[`test_tools.py`](../native/claude/test_tools.py) drives the real CLI against a scripted Anthropic
endpoint. `test_file_edit_round_trip_changes_the_workspace` creates a file in the CLI's local
workspace, emits `Read`, `Edit`, `Read` model calls, and checks the changed file and returned content.
`test_parallel_shell_tools_report_both_streams_and_exit_codes` exercises real shell commands.
The [launcher](../native/claude/testing/harness.py) starts the CLI with that workspace as its cwd.

The driver's [`allow_tool`](../native/claude/driver.py) answers a permission request with
`behavior: allow` and `updatedInput`; this does not supply an alternate executor or a tool result.
The separate `mcp_response` control does carry driver-hosted tool results.
[`test_driver_hosted_tool_is_declared_and_called`](../native/claude/test_driver_tools.py) declares
`mcp__driver__echo` at initialization, handles its `mcp_message` JSON-RPC calls in the driver, and
asserts that the next model request contains `ECHO: hi`. This demonstrates the custom-tool seam,
without proving a remote Sandbox implementation or replacement of built-ins.

The 2.1.252 bundle's `--tools <tools...>` description explicitly allows `""` to disable built-ins.
That permits a central CLI with driver-hosted remote tools, subject to testing that combined setup.
It retains Claude's sampling and transcript machinery but requires Agentplane to supply the desired
tool semantics. `CLAUDE_CODE_SHELL{,_PREFIX}` interception alone leaves filesystem tools and direct
helpers local ([shell and memory isolation evidence](../docs/harness_tool_memory_isolation.md)).

## Native remote tools: the version matters

### 2.1.252: the public gate is compiled off

At byte offset `178011990` near `CLAUDE_CODE_REMOTE_TOOLS_SESSION_CHANNEL`, the embedded source has:

```js
async function RX() {
  return iv() && (await Ec());
}
function mDe() {
  return iv() && Ws();
}
function iv() {
  return !1;
}
```

The neighboring code reads `CLAUDE_CODE_REMOTE_TOOLS_{SERVE,POLICY,SESSION_CHANNEL}` and reports
`external_build`. Setting those variables cannot change the literal `false` gate. This establishes
the inspected build's entry-point limitation, not a permanent limitation of Claude Code.

### 2.1.296: a local switch plus account and transport gates

The embedded `chunk-4r70ar86.js` changes the availability predicate:

```js
function zre() {
  return a.CLAUDE_CODE_REMOTE_TOOLS_FORWARD === !0;
}
```

`chunk-dp4xqs6t.js` declares that variable with `H.bool()` and reads it through the environment
registry. `CLAUDE_CODE_REMOTE_TOOLS_FORWARD=1` is therefore a candidate way to enable the local
forwarding predicate. It has not been exercised here. It is not sufficient evidence of a working
remote backend:

- `chunk-md7g8y4m.js`: `dR()`/`xf()` consult `tengu_violin_wood`; `R2()`/`fW()` in
  `chunk-qwvy7ma3.js` combine that account flag with the local predicate.
- `chunk-ztdmpyqt.js`: bridge startup's `CLr({cloudClient})` also requires `tengu_violin_wood`,
  plus `tengu_violin_willow` when `cloudClient` is true. The local environment switch does not
  replace these checks. No supported account-flag override was established.
- Locate the device-bridge module by its `V0t` function or the literal
  `CLAUDE_REMOTE_TOOLS_BRIDGE_URL`. It checks readiness, egress/provider
  policy, resolved account and organization identity, and trusted-device requirements. Its
  `Ve()` honors `CLAUDE_REMOTE_TOOLS_BRIDGE_URL`; `AL()` selects the bridge path when
  `CLAUDE_CODE_REMOTE_TOOLS_SESSION_CHANNEL` is false. A redirectable URL does not establish a
  self-hosted handshake, account service or sampler implementation.

The device bridge builds an MCP server (`chunk-f1j5pmqs.js`, `createDeviceMcpServer`) whose
`tools/list` reads registry definitions and whose `tools/call` invokes the registered tool. Calls
carry session identity and cancellation; mismatched sessions and unattested requests can be refused.
`remoteToolServingGroupProvider` loads native served tools alongside a separate MCP passthrough
group. This is a real execution-serving path, unlike Remote Control's conversation transport.

### Does this retain `CLAUDE.md` behavior?

Partly shared machinery is visible; complete instruction parity is unverified.
`chunk-n9tjv0a2.js` exports `createServingKit` and `remoteToolServingGroupProvider`. Its `ro()` builds a
native tool context using per-caller `readFileState` and `toolState`, a project cwd and permission
context. Its default executor `so()` invokes `Kn()`, which runs the native `Xj` tool pipeline. This
supports investigating native reads and edits rather than assuming an SSH wrapper is necessary.

However, executing `Read` and supplying the next model request's instructions are different steps.
The 2.1.252 local pipeline produces `nested_memory` attachments and tracks loaded paths
(`Zln`, near byte offset `183577375`); it also has read-before-edit checks. In 2.1.296's served-call
collector, `Kn()` handles a `read_truncation_notice`, then skips attachments whose type does not start
with `hook_`. Its constructed context has `messages: []` and a no-op `enqueuePendingMemoryUpdate`.
These observations do not establish that nested instructions reach the remote sampler. Project
sync or cloud-side loading might supply them separately.

A conclusive scripted probe must put unique instructions in root and nested `CLAUDE.md` files,
read the nested file remotely, and inspect the **next model request** for the instructions, once and
in scope. Repeat after resume and with different caller Sessions; check read-before-edit state,
path mapping and instructions outside synced roots. A successful file read alone is insufficient.

For reproducibility, inspect the matching npm tarball or Nix binary with Python `mmap`: find the
ASCII anchors above and extract the surrounding NUL-terminated embedded JavaScript. Minified
function names and offsets are version-specific. Do not publish the whole binary or session data.

## Remote Control and RemoteIO answer a different question

[Remote Control inspection](claude_remote_control.md) covers 2.1.245: conversation SDK frames travel
between the UI/controller and a worker CLI. The
[2.1.292 RemoteIO spike](../native/claude/x/remote_io/README.md) similarly moves user/control ingress
and SDK-output transport. Its crash probes retain native files and use `--resume`. Neither proves
that sampling, built-in tool execution and persistence can be placed independently. Do not reuse the
older Remote Control endpoint conclusions for 2.1.296's separate remote-tools bridge.

## A custom sampler in front of CLIProxyAPI

This is a separate route from retaining the Claude CLI and forwarding its tools. A custom harness
could send Anthropic Messages requests through CLIProxyAPI, own the model/tool loop and Postgres
persistence, and execute tools in a Pod or VM. The provider sees model API requests; implementing
Claude's stdio `stream-json` controller protocol is only necessary if an Agentplane caller needs
that compatibility. An SDK-looking wire identity alone does not recreate native tool behavior.

At the repository's pinned CLIProxyAPI revision:

- [`DetectClaudeCodeRequest`][detection] recognizes CLI and `sdk-cli` identities using several
  signals, including headers and request metadata; generic SDK entrypoints are treated differently.
  One User-Agent is not the whole detection contract.
- [`resolveClaudeWirePolicy` and `applyCloakingInternal`][cloak] implement configurable cloaking for
  non-native callers, with CLI-shaped system content and request continuity. Confirmed native clients
  bypass cloaking. [`resolveClaudeFingerprintPolicy`][fingerprint] separates CLI wire identity from
  actual OAuth lifecycle handling.
- The [executor][execute] translates incoming requests before applying the wire policy, and can
  alias client tools into MCP-shaped names. [Header construction][headers] handles provider auth
  and beta headers. The [client-detection tests][detection-tests] and
  [cloaking/cache tests][cloak-tests] expose concrete compatibility cases; they were read, not run here.

This is source evidence for the user's proposed facade, not a live subscription-access or provider
acceptance result. A spike should compare ordinary Messages plus proxy cloaking with any explicitly
needed CLI/SDK identity. Capture returned tool names, continuation/thinking content, streaming and
cache behavior. The custom harness still owns instruction loading, editing, processes, compaction,
interrupts and recovery. Codex-only central execution remains an independent option if the Claude
path costs too much.

[detection]: https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/helps/claude_client_detection.go#L131
[detection-tests]: https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/helps/claude_client_detection_test.go
[cloak]: https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/claude_executor_cloaking.go#L1595
[fingerprint]: https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/claude_fingerprint_policy.go#L95
[execute]: https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/claude_executor_execute.go#L83
[headers]: https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/claude_executor_request.go#L1173
[cloak-tests]: https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/claude_cloaked_cache_repro_test.go
