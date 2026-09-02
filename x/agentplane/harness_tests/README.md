# Scripted harness tests

Behavioral tests of the pinned Claude Code and Codex app-server binaries, each written as one
interleaved script: poke the harness over stdio, take the upstream model request and assert the
markers that matter, answer it with a built stream, then assert the native frames and workspace
effects. No model, no credentials, no recorded fixtures.

```sh
bbr test //x/agentplane/harness_tests/...
```

The supported test environment is the RBE worker, whose Ubuntu userland runs the pinned binaries
as they ship; a NixOS host running Bazel locally reaches the same result through nix-ld. Running
the tests outside Bazel with a Nix-built Python is not supported: the binaries find no
`/lib64` loader there.

## Pieces

- `scripted_upstream.py`: a loopback model endpoint the test drives one request at a time.
  `next_request()` hands over what the harness sent; `respond()` answers with a `Stream` (SSE
  packets, optionally truncated after a named packet to simulate a lost connection, or held open),
  a `Body` (a non-streaming JSON response), or `Refuse` (close before any bytes). `always()`
  registers a standing rule for traffic the harness generates on its own, such as a retry storm.
  A request no step answers holds its connection open, so a missing step surfaces as a native
  timeout rather than a vacuous pass.
- `claude/anthropic_sse.py`, `codex/responses_sse.py`: response builders in the two SSE dialects,
  shaped after real routes. Thinking blocks and reasoning items are part of the vocabulary because
  the harness has to echo them back.
- `claude/requests.py`, `codex/requests.py`: the request bodies as typed markers (tool roster,
  system prompt or instructions, texts by role, tool results, thinking blocks, cache key).
- `claude/frames.py`, `codex/frames.py`: assertions over the native output frames.
- `claude/harness.py`, `codex/harness.py`: the pinned binary wired to the upstream; provider
  fixtures live in each `conftest.py`.

The four test modules per provider cover plain turns and resume, tool round trips, input and
control during an active turn, and upstream connection loss. The scenarios drive the harnesses
through <../native/README.md>; features each harness has beyond what these tests exercise are
listed in <../native/docs/protocol_roster.md>.

## When a binary pin changes

The tests pin observed behavior of one build. After a bump, run the live probe in
<../capture/README.md> for the scenario that fails, compare what the harness now sends with the
scripted expectations, and update the script. The probe's raw logs are the reference for that
edit; they are never test inputs.

## Gotchas

- Every scripted message, item, and response carries a fresh id: both harnesses key their
  transcripts by id and silently merge repeats.
- Codex turns on code mode (one JS `exec` tool) for model ids in its built-in catalog; the tests
  use an id it does not know so the classic function-call shape is what gets asserted.
- Claude's retry after a lost stream is a non-streaming request, so the retry step answers with
  `message_body`, not `message_stream`.
