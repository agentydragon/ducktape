# Scripted harness tests

Behavioral tests of the pinned Claude Code and Codex app-server binaries, each written as one
interleaved script: poke the harness over stdio, take the upstream model request and assert the
markers that matter, answer it with a built stream, then assert the native frames and workspace
effects. No model, no credentials, no recorded fixtures.

```sh
bbr test //agentplane/harness_tests/...
```

The supported test environment is the RBE worker, whose Ubuntu userland runs the pinned binaries
as they ship; a NixOS host running Bazel locally reaches the same result through nix-ld. Running
the tests outside Bazel with a Nix-built Python is not supported: the binaries find no
`/lib64` loader there.

## Pieces

- `claude/messages.py` and `codex/responses.py`: API-shaped loopback endpoints. Tests await a
  parsed request exchange and explicitly send, close, respond to, abort, or await client closure.
- `model_endpoint.py`: private HTTP lifecycle and fixture teardown that rejects every unsettled
  exchange.
- `native/{claude,codex}/testing/async_run.py`: harness-specific async context managers that own launch
  and handshake. Each exposes typed input/turn handles and independent event cursors over the
  append-only native trace, so observing one receipt cannot drop another.
- `claude/anthropic_sse.py`, `codex/responses_sse.py`: response builders in the two SSE dialects,
  shaped after real routes. Thinking blocks and reasoning items are part of the vocabulary because
  the harness has to echo them back.
- `claude/requests.py`, `codex/requests.py`: the request bodies as typed markers (tool roster,
  system prompt or instructions, texts by role, tool results, thinking blocks, cache key).
- `claude/frames.py`, `codex/frames.py`: assertions over the native output frames.
- `claude/harness.py`, `codex/harness.py`: the pinned binary wired to the upstream; harness
  fixtures live in each `conftest.py`.

## Behavioral coverage

These are tested scenarios for the versions pinned in `MODULE.bazel`, not a claim that every native
protocol capability is characterized. Each module documents the behavior its assertions cover.

| Scenario                        | Claude Code                                                                                  | Codex app-server                                                                                                 |
| ------------------------------- | -------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Turns and resume                | [turns](claude/test_turns.py), [compaction](claude/test_compaction.py)                       | [turns](codex/test_turns.py), [instructions](codex/test_instructions.py), [compaction](codex/test_compaction.py) |
| Input and control during a turn | [queued input and interrupt](claude/test_active_turn.py)                                     | [joined input, steer and interrupt](codex/test_active_turn.py)                                                   |
| Upstream connection loss        | [retry fallback and exhaustion](claude/test_connection_loss.py)                              | [retry notices and exhaustion](codex/test_connection_loss.py)                                                    |
| Tools and MCP                   | [tool round trips](claude/test_tools.py), [driver-hosted tools](claude/test_driver_tools.py) | [tool round trips](codex/test_tools.py), [dynamic tools](codex/test_dynamic_tools.py)                            |
| Hook decisions                  | [PreToolUse allow/deny](claude/test_hooks.py)                                                | [PreToolUse allow/deny](codex/test_hooks.py)                                                                     |
| Native children                 | [Agent lifecycle and follow-up](claude/test_tools.py)                                        | [v1 spawn/wait](codex/test_tools.py), [v2 persisted discovery](codex/test_v2_discovery.py)                       |
| Root identity and multiplexing  | [first-input identity](claude/test_root_identity.py); multiple roots remain uncharacterized  | [two root threads over one app-server](codex/test_multiplexing.py)                                               |

The Claude suite exercises one stream-json root session at a time. The Codex multiplexing test is the
runtime characterization for two independently controlled root threads; its pinned source
references and untested crash-recovery boundary are explicit in that module's docstring. Neither
suite decides whether Agentplane should change its process/Session model. The current open design question is in
[runner delivery and harness identity](../docs/runner_delivery_and_harness_design.md#harness-process-store-and-session-identity).

The harness runs drive the binaries through <../native/README.md>. The
[protocol roster](../native/docs/protocol_roster.md) lists protocol surfaces used or left unused by
the fixtures; it is not a claim that every listed feature is tested.

## When a binary pin changes

The tests pin observed behavior of one build. After a bump, run the live probe in
<../capture/README.md> for the scenario that fails, compare what the harness now sends with the
scripted expectations, and update the script. The probe's raw logs are the reference for that
edit; they are never test inputs.

## Cleanup ownership

`AsyncNativeProcess` owns exceptional process-group cleanup and trace draining. Scripts let
assertions and cancellation propagate through the harness context manager; they do not add
failure-only `try`/`crash()` wrappers. Successful exit remains graceful. Explicit `crash()` calls
are scenario steps for parent-process death and intentionally retain their distinct semantics.

## Gotchas

- Every scripted message, item, and response carries a fresh id: both harnesses key their
  transcripts by id and silently merge repeats.
- Codex turns on code mode (one JS `exec` tool) for model ids in its built-in catalog; the tests
  use an id it does not know so the classic function-call shape is what gets asserted.
- Claude's `--name` fixture option suppresses the separate title-generation model request.
- Claude's retry after a lost stream is a non-streaming request, so the retry step answers with
  `message_body`, not `message_stream`.
