# ScriptHandler: Generator-Based Agent Handler

## Problem

Bootstrap and pre-scripted agent behaviors are currently split across multiple
handler classes with different patterns:

- `BootstrapHandler` — injects one `FunctionCallItem`, monitors its result via
  `on_tool_result_event()`, raises on failure. Single-call only.
- `SequenceHandler([InjectItems(...)])` — injects a list of calls
  fire-and-forget, no result monitoring.
- `TypedBootstrapBuilder` — builds typed `FunctionCallItem`s with call ID
  generation and optional server introspection.
- `docker_exec_call()` — free function to build exec calls.

These pieces are scattered across `agent_core/handler.py`,
`agent_pkg/host/bootstrap_handler.py`, and `mcp_infra/bootstrap/bootstrap.py`.
Writing a multi-step bootstrap (e.g., "run init, validate result, then read a
resource") requires manually wiring handler state, matching call IDs, and
implementing `on_tool_result_event`.

## Goal

A single generator-based handler (`ScriptHandler`) that wraps a script
generator, analogous to how `GeneratorRunner` wraps a `PlayGen` generator into
an `OpenAIModelProto`. The generator yields agent actions (tool calls, messages)
and receives transcript events (tool results). Sequential code replaces
hand-rolled state machines.

A single builder class (`ScriptBuilder`) that merges `TypedBootstrapBuilder`
and `docker_exec_call()` with `yield from`-composable roundtrip helpers,
analogous to how `MCPDecoratorMock` puts roundtrip helpers on the mock class.

## Design

### Generator protocol

The script generator follows batch semantics: each `yield` sends items out and
receives all transcript events since the previous yield.

```
yield: list[ScriptItem] | None    →  items to inject (None = no action)
send:  list[TranscriptEvent]      →  events since last yield
```

- First `yield` must be `None` (prime yield — receive pre-existing events,
  emit nothing). Same pattern as PlayGen's initial `yield None`.
- Subsequent yields emit items to inject and receive the resulting events.
- `StopIteration` (generator returns) → handler becomes passive (`NoAction()`
  forever), LLM takes over.
- Exceptions propagate through the agent loop.
- `yield from` composes sub-generators for reusable patterns (`exec_ok`,
  `call_roundtrip`, etc.).

### Type definitions

```python
ScriptItem = SystemMessage | UserMessage | FunctionCallItem

# Only ToolCallOutput for now; extend if scripts need to react to other events.
TranscriptEvent = ToolCallOutput

ScriptGen = Generator[
    Sequence[ScriptItem] | None,   # yield
    list[TranscriptEvent],          # send
]

SubScriptGen[T] = Generator[
    Sequence[ScriptItem] | None,   # yield
    list[TranscriptEvent],          # send
    T,                              # return
]
```

### ScriptHandler (the handler wrapper)

Mirrors `GeneratorRunner`: primes the generator with `next()`, translates
between BaseHandler hooks and the generator protocol.

- `on_tool_result_event(evt)` → buffer event
- `on_before_sample()` → `send(buffered_events)` to generator, wrap yielded
  items into `InjectItems`, or `NoAction()` if generator returned `None` or
  is exhausted

### ScriptBuilder (the item factory + roundtrip helpers)

Replaces `TypedBootstrapBuilder` and `docker_exec_call()`. Inherits from
`ItemFactory` (which provides `next_call_id()`, `tool_call()`).

**Call builders** (from `TypedBootstrapBuilder`):

- `call(server, tool, payload)` → namespaced `FunctionCallItem`
- `docker_exec(runtime, cmd, timeout_ms=)` → exec `FunctionCallItem`
- `read_resource(resources, server, uri, max_bytes=)` → resource read
  `FunctionCallItem`

**Roundtrip sub-generators** (new, for `yield from`):

- `exec_roundtrip(runtime, cmd)` → yield call, return `BaseExecResult`
- `exec_ok(runtime, cmd)` → yield call, validate exit 0 + no truncation,
  return `BaseExecResult`
- `call_roundtrip(server, tool, payload, output_type)` → yield call, return
  parsed output

**Dropped**: `for_server()` introspection and `introspect_server_models()`.
Type mismatches surface at runtime anyway.

### Parallel table with PlayGen

| PlayGen                                                | ScriptHandler                                     |
| ------------------------------------------------------ | ------------------------------------------------- |
| `GeneratorRunner` wraps `PlayGen` → `OpenAIModelProto` | `ScriptHandler` wraps `ScriptGen` → `BaseHandler` |
| `ItemFactory` / `MCPDecoratorMock`                     | `ScriptBuilder`                                   |
| `tool_roundtrip(call, T)`                              | `exec_roundtrip` / `call_roundtrip`               |
| `extract_call_output(req, call, T)`                    | `find_tool_result(events, call_id, T)`            |
| `next(gen)` primes to first yield                      | `next(gen)` primes, asserts `None`                |
| `gen.send(request)` → response items                   | `gen.send(events)` → script items                 |
| `StopIteration` → error (mock exhausted)               | `StopIteration` → passive (script done)           |

## Migration

### Callers to migrate

| Caller                              | Current pattern                                                                                                  | New pattern                                                       |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `editor_agent/host/agent_runner.py` | `TypedBootstrapBuilder()` + `docker_exec_call()` + `BootstrapHandler(call)`                                      | `ScriptBuilder()` + `ScriptHandler(editor_bootstrap(b, runtime))` |
| `git_commit_ai/agent_backend.py`    | `TypedBootstrapBuilder.for_server()` + `make_commit_bootstrap_calls()` + `SequenceHandler([InjectItems(calls)])` | `ScriptBuilder()` + `ScriptHandler(git_ro_bootstrap(b, ...))`     |
| `agent_core/test_handlers.py`       | Tests for `TypedBootstrapBuilder` + `SequenceHandler`                                                            | Tests for `ScriptBuilder` + `ScriptHandler`                       |

### Files to delete after migration

- `agent_pkg/host/bootstrap_handler.py` — `BootstrapHandler` class
- `mcp_infra/bootstrap/` — entire directory (`bootstrap.py` +
  `BUILD.bazel`). Contains `TypedBootstrapBuilder`, `docker_exec_call()`,
  `introspect_server_models()`, `DEFAULT_BOOTSTRAP_ITEM_TIMEOUT_MS` — all
  become dead.
- `agent_server/docs/bootstrap_type_safety.md` — documents the introspection
  pattern being removed

### Files to update

- `mcp_infra/mounted.py:34-36` — docstring example references
  `TypedBootstrapBuilder`, update to `ScriptBuilder`

### Files to keep

- `agent_core/handler.py` — `SequenceHandler` stays (general-purpose, could
  have non-InjectItems uses)
- `mcp_infra/compositor/resources_server.py` — `ResourcesReadArgs` stays
  (core input type for resources tool; only the bootstrap helper that
  constructed it goes away, `ScriptBuilder.read_resource` will use it)

### Additional dead files

- `agent_pkg/host/init_runner.py` — `InitFailedError` and `run_init_script`
  are only used by `bootstrap_handler.py`. Both become dead. `exec_ok` will
  define its own error type in `script_handler.py` (e.g. `ScriptError`).

### Handlers considered but not migrated

`NotificationsHandler`, `ServerModeHandler`, `GraderDriftHandler` — these
poll external state (queues, DB views), not transcript events. Their
decisions aren't driven by what the agent did but by what happened
externally. Poor fit for the generator protocol.

## Implementation plan

### Phase 1: Core — ScriptBuilder + ScriptHandler + tests

New files:

- `agent_core/script_handler.py` — `ScriptHandler`, `ScriptGen`,
  `SubScriptGen`, `TranscriptEvent`, `ScriptItem`, `find_tool_result`
- `agent_core/script_builder.py` — `ScriptBuilder` (inherits `ItemFactory`,
  absorbs `TypedBootstrapBuilder` + `docker_exec_call`)
- `agent_core/test_script_handler.py` — unit tests

Tests to write:

- [ ] Prime yield must be `None` (error if not)
- [ ] Single call injection + result extraction via `find_tool_result`
- [ ] `exec_ok` validates exit 0, raises `ScriptError` on non-zero
- [ ] `exec_ok` raises on truncated output
- [ ] Generator return → handler returns `NoAction()` forever
- [ ] Generator exception propagates
- [ ] Multiple serial yields (multi-step script)
- [ ] Parallel calls (multiple items in one yield)
- [ ] `yield from` sub-generator composition
- [ ] `call_roundtrip` with typed output parsing

### Phase 2: Migrate editor_agent

- [ ] Replace `BootstrapHandler` + `TypedBootstrapBuilder` +
      `docker_exec_call` with `ScriptBuilder` + `ScriptHandler`
- [ ] Update `editor_agent/host/BUILD.bazel` deps
- [ ] Verify `bazel build --config=check //editor_agent/...` passes

### Phase 3: Migrate git_commit_ai

- [ ] Replace `SequenceHandler([InjectItems(...)])` +
      `TypedBootstrapBuilder.for_server()` +
      `make_commit_bootstrap_calls()` with `ScriptBuilder` +
      `ScriptHandler(git_ro_bootstrap(...))`
- [ ] Update `git_commit_ai/BUILD.bazel` deps
- [ ] Verify `bazel build --config=check //git_commit_ai/...` passes

### Phase 4: Migrate tests + delete old code

- [ ] Rewrite `agent_core/test_handlers.py` bootstrap tests to use
      `ScriptBuilder` + `ScriptHandler`
- [ ] Delete `agent_pkg/host/bootstrap_handler.py`
- [ ] Delete `agent_pkg/host/init_runner.py`
- [ ] Delete `mcp_infra/bootstrap/` directory entirely
- [ ] Delete `agent_server/docs/bootstrap_type_safety.md`
- [ ] Update docstring in `mcp_infra/mounted.py` referencing
      `TypedBootstrapBuilder`
- [ ] Update BUILD.bazel files to remove dead deps
- [ ] Verify `bazel build --config=check //...` passes
- [ ] Verify `bazel test //...` passes

## Definition of done

- [ ] `ScriptHandler` and `ScriptBuilder` exist with unit tests covering the
      protocol (prime, inject, result extraction, exhaustion, error propagation,
      `yield from` composition)
- [ ] All three callers (`editor_agent`, `git_commit_ai`, `test_handlers`)
      migrated
- [ ] `BootstrapHandler`, `init_runner.py`, `TypedBootstrapBuilder`,
      `docker_exec_call()`, `introspect_server_models()`,
      `mcp_infra/bootstrap/` deleted
- [ ] `bazel build --config=check //...` passes
- [ ] `bazel test //...` passes (excluding pre-existing Docker-on-RBE failures)
