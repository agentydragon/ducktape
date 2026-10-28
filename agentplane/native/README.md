# Native harness drivers

Drives the real Claude Code and Codex app-server binaries over their own stdio protocols:
the test-only `testing/async_process.py` owns the pipes and records every frame as JSONL; `claude/`
and `codex/` hold the harness-specific wire models (`wire.py`, with Anthropic content blocks in
`claude/blocks.py`), frame constructors (`driver.py`), test-only async run facades under `testing/`,
and the launch `command()`/`environment()` every consumer shares. `claude/testing/async_run.py` and
`codex/testing/async_run.py` deliberately remain harness-specific: there is no harness-neutral
facade.

The wire models describe only the frames a consumer reads, as observed from the pinned builds; a
frame, event, or item of a kind they do not describe decodes to a named `Unknown*` variant rather
than failing, since the harness is the writer and a newer pin may add kinds. Inbound frames are
parsed with `wire.parse_frame`; outbound frames are the models the drivers construct, serialized at
the pipe.

Three consumers: the live-capture probe in <../capture/README.md>, which runs a scenario against a
real model; the [scripted harness tests](#scripted-harness-tests), which drive the same run facades
against a loopback model the test controls; and the runner in <../runner/README.md>, which reuses
the frame constructors and launch configuration behind its harness-neutral protocol over its own
supervised native-process pipe (`../runner/supervision/`).

## Scripted harness tests

These behavioral tests drive the pinned Claude Code and Codex binaries as interleaved scripts: send
stdio frames, inspect the model request, answer through a loopback endpoint, and assert native frames
and workspace effects. They need no model credentials or recorded fixtures.

```sh
bbr test //agentplane/native/...
```

The supported environment is the RBE worker, whose Ubuntu userland runs the pinned binaries as they
ship. Running the tests outside Bazel with a Nix-built Python is unsupported because the binaries
find no `/lib64` loader there. The tests sit beside the harness-specific native unit tests, but only
integration tests request the `claude` or `codex` launch fixture; those fixtures are not autouse.
Shared workspace and trace fixtures live in `native/conftest.py`, and the model endpoint is under
`native/testing/`. Each harness's endpoint and request/response vocabulary lives under its own
`testing/` package.

### Behavioral coverage

Child discovery, live-item attribution and recovery findings are summarized in
[Native child Session evidence](docs/child_sessions.md), with explicit transport and configuration
boundaries. These tests do not implement public child Sessions or child control.

Each test module documents its assertions. The main scenarios covered are:

| Scenario                        | Claude Code                                                                                               | Codex app-server                                                                                                 |
| ------------------------------- | --------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Turns and resume                | [turns](claude/test_turns.py), [compaction](claude/test_compaction.py)                                    | [turns](codex/test_turns.py), [instructions](codex/test_instructions.py), [compaction](codex/test_compaction.py) |
| Input and control during a turn | [queued input and interrupt](claude/test_active_turn.py)                                                  | [joined input, steer and interrupt](codex/test_active_turn.py)                                                   |
| Upstream connection loss        | [retry fallback and exhaustion](claude/test_connection_loss.py)                                           | [retry notices and exhaustion](codex/test_connection_loss.py)                                                    |
| Tools and MCP                   | [tool round trips](claude/test_tools.py), [driver-hosted tools](claude/test_driver_tools.py)              | [tool round trips](codex/test_tools.py), [dynamic tools](codex/test_dynamic_tools.py)                            |
| Hook decisions                  | [PreToolUse allow/deny](claude/test_hooks.py)                                                             | [PreToolUse allow/deny](codex/test_hooks.py)                                                                     |
| Native children                 | [Agent lifecycle and follow-up](claude/test_tools.py)                                                     | [v1 spawn/wait](codex/test_tools.py), [v2 persisted discovery](codex/test_v2_discovery.py)                       |
| Root identity and multiplexing  | [first-input identity](claude/test_root_identity.py), [one root per process](claude/test_multiplexing.py) | [two root threads over one app-server](codex/test_multiplexing.py)                                               |

On Claude Code 2.1.252, an inbound `user` frame carrying a different `session_id` gets a lifecycle
receipt for the process's root and stays queued while that root is active; it does not start a
concurrent model request for the candidate ID. Two separately launched processes, each with its own
workdir and config directory, can hold concurrent requests, use separate model settings, and keep
one process's interrupt from cancelling the other's request. Different credentials, multiple
processes sharing one config directory, and concurrent multi-process crash recovery remain untested.
The Codex multiplexing test characterizes two independently controlled root threads; its pinned
source references and untested crash-recovery boundary are in that module. Neither suite decides
whether Agentplane should change its process or Session model. The open design question is in
[runner delivery and harness identity](../docs/runner_delivery_and_harness_design.md#harness-process-store-and-session-identity).

The [protocol roster](docs/protocol_roster.md) lists the protocol surfaces used or left unused by
the fixtures; it does not claim every listed feature is tested. When a binary pin changes, use the
[live-capture probe](../capture/README.md) for the scenario that fails, compare what the harness now
sends with the scripted expectations, and update the script. Capture logs are references for repair,
never test inputs.

`AsyncNativeProcess` owns exceptional process-group cleanup and trace draining. Scripts let
assertions and cancellation propagate through the harness context manager; they do not add
failure-only `try`/`crash()` wrappers. Successful exit remains graceful. Explicit `crash()` calls
are scenario steps for parent-process death and keep their distinct semantics.

Gotchas: every scripted message, item and response needs a fresh id because both harnesses key
transcripts by id and silently merge repeats. Codex turns on code mode (one JavaScript `exec` tool)
for ids in its built-in catalog; the tests use an unknown id to exercise the classic function-call
shape, and `runner/test_codex_code_mode.py` serves GPT-6 Luna's entry to cover code mode. Claude's `--name` fixture option suppresses a separate title-generation request, and its
retry after a lost stream is non-streaming, so the retry step answers with `message_body`, not
`message_stream`.

## Protocol references

What each harness exposes on its protocol, and which behavior the tests cover:
<docs/protocol_roster.md>.

Version-pinned Claude findings that remain source-only or come from an older capture:
<docs/claude_source_findings.md>.
