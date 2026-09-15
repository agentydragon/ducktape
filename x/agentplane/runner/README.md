# Agentplane runner

A gRPC service that runs one native harness per session, Claude Code or Codex, and exposes both
through one protocol. The contract is in <SPEC.md>; the wire definition is `protocol.proto`.

```sh
bbr test //x/agentplane/runner/...
```

## Layout

- `protocol.proto`: the contract. Its messages (`protocol_pb2`) are generated through standard
  `proto_library` and `py_proto_library` rules; the service-only `protocol_pb2_grpc` module uses
  the narrow `py_grpc_service_library` fallback in `devinfra/python/grpc.bzl` with the pinned
  `grpcio-tools` and `mypy-protobuf` plugins.
- `service.py`: the `Attach` RPC, session lookup, and `serve()`; `main.py` is the process entry
  point, configured by flags and credentialed from its environment.
- `session.py`: one session's log, harness process, and derived state; `event_log.py` is the
  append-only JSONL log; `store.py` the session record on disk.
- `journal_file.py`: Event and command appends flush and fsync before publishing state, with
  directory fences for newly created state paths and renamed session metadata. A storage error
  poisons the writer until it is reopened for recovery.
- `journal_lines.py`: Event/command journal recovery. The terminating newline completes a record;
  an unterminated final fragment is truncated and synced before subsequent appends. Complete
  records are decoded and validated by their owning log.
- `harness_process.py`: one native harness child, its pipes, line framing, and exit; no protocol
  knowledge.
- `config.py`: the runner-owned launch configuration, one `*Launch` per harness (binary, endpoint,
  credential) under `RunnerConfig`; none of it crosses the protocol.
- `initialization.py`: the durable, replayable log of the one bootstrap initialization a sandbox
  may select.
- `claude.py`, `codex.py`: the adapters, one per harness, behind `adapter.py`. They parse frames
  with the wire models and reuse the frame constructors and launch configuration in
  <../native/README.md>.
- `client.py`: a typed client over one attachment plus `list_sessions`, used by the tests and
  meant for the Agentplane service.

## Tests

Each test is one interaction script written against the client and run against both harnesses;
the parametrized `model` fixture is the only place that knows the model API dialect. Harness
fixtures live in `testing/`: `scripted_model.py` is the neutral vocabulary (`Text`, `Reasoning`,
`ShellCall`, and the request markers), `claude_model.py` and `codex_model.py` speak the two
dialects, and `launches.py` wires the pinned binaries to a scripted upstream. `test_restart.py`
runs the runner as its own process so a crash takes its harnesses with it. `test_image.py` runs
the built runner image as a container (Docker, so on RBE) through one scripted turn per harness;
`test_image_packaging.py` inspects its OCI layout for the harnesses, their tools, and the
entrypoint.

`test_event_durability.py` records file contents and directory entries only at successful fsync
boundaries, then rebuilds a fresh storage image without unsynced writes. It verifies replay and
publication ordering, failed fences, command identity/outcomes, and session metadata discovery.
This is an explicit power-loss model; it does not validate a physical device's fsync behavior.
`test_journal_process.py` separately kills a real journal-writing process and replays its published
Events from a new process. Process SIGKILL alone does not simulate loss of the kernel write cache.
