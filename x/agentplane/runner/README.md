# Agentplane runner

A gRPC service that runs one native harness per session, Claude Code or Codex, and exposes both
through one protocol. The contract is in <SPEC.md>; the wire definition is `protocol.proto`.

```sh
bbr test //x/agentplane/runner/...
```

## Layout

- `protocol.proto`: the contract. `protocol_pb2*` are generated at build time by the
  `py_grpc_library` macro in `devinfra/python/grpc.bzl`, from `grpcio-tools` with `mypy-protobuf`
  stubs.
- `service.py`: the `Attach` RPC, session lookup, and `serve()`; `main.py` is the process entry
  point, configured by flags and credentialed from its environment.
- `session.py`: one session's log, harness process, and derived state; `event_log.py` is the
  append-only JSONL log; `store.py` the session record on disk.
- `claude.py`, `codex.py`: the adapters, one per harness, behind `adapter.py`. They parse frames
  with the wire models and reuse the frame constructors and launch configuration in
  <../native/README.md>.
- `client.py`: a typed client over one attachment, used by the tests and meant for the Agentplane
  service.

## Tests

Each test is one interaction script written against the client and run against both harnesses;
the parametrized `model` fixture is the only place that knows the model API dialect. Provider
fixtures live in `testing/`: `scripted_model.py` is the neutral vocabulary (`Text`, `Reasoning`,
`ShellCall`, and the request markers), `claude_model.py` and `codex_model.py` speak the two
dialects, and `launches.py` wires the pinned binaries to a scripted upstream. `test_restart.py`
runs the runner as its own process so a crash takes its harnesses with it.
