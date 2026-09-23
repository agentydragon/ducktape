# Agentplane runner

A gRPC service that runs one native harness per session, Claude Code or Codex, and exposes both
through one protocol. The contract is in <SPEC.md>; the wire definition is `protocol.proto`.

```sh
bbr test //agentplane/runner/...
```

## Layout

- `protocol.proto`: the contract. Its messages (`protocol_pb2`) are generated through standard
  `proto_library` and `py_proto_library` rules; the service-only `protocol_pb2_grpc` module uses
  the narrow `py_grpc_service_library` fallback in `devinfra/python/grpc.bzl` with the pinned
  `grpcio-tools` and `mypy-protobuf` plugins.
- `service.py`: the `Attach` RPC, session lookup, and `serve()`; `main.py` is the process entry
  point, configured by flags and credentialed from its environment.
- `session.py`: one session's harness process and derived state; `store.py` the session metadata
  and durable directory creation.
- `journal.py`: one SQLite database per durable runner session. SQLAlchemy/aiosqlite transactions
  commit command admission/outcome state with their exact protobuf `EventEntry` bytes. Coalesced
  receipts link all originating commands to one Event in the same transaction. Publication and
  follower wakeup happen after commit; storage failure or cancelled commit stops the writer until
  recovery. `observation.py` maps harness-neutral observations to the generated Event vocabulary.
- `harness_process.py`: one native harness child, its pipes, line framing, and exit; no protocol
  knowledge. `harness_supervisor.rs` retains the state-owner descriptor across runner death,
  forwards shutdown to the native process group, and reaps the native leader.
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

`test_journal.py` gates real SQLite commits and injects failure/cancellation before or after commit,
checking transaction visibility, atomic coalesced receipts, immutable ids, and replay without cursor
reuse. `test_journal_process.py` kills a real writer and replays its exact published prefix from a
new process; `test_restart.py` also exercises the real runner and both native harnesses. These are
not physical power-loss tests. `test_store.py` retains a separate fsync-boundary storage image for
session metadata and directory discovery.

## SQLite storage

The connection uses `journal_mode=DELETE` and `synchronous=EXTRA`; publication relies on SQLite's
[durable rollback-journal commits](https://sqlite.org/pragma.html#pragma_synchronous) and storage honoring sync.
An async lock owns each complete transaction; the driver's per-statement queue alone does not.
Explicit `BEGIN IMMEDIATE` reserves the writer before reading ids/cursors; no transaction spans
harness or network I/O. One retained connection serves the existing single runner-session writer.
That SQLite transaction is not the native-execution fence: a runner owns its whole retained state
directory through a nonblocking lifetime lock on the fixed `.agentplane-runner-owner` inode before
opening a journal, listening, or launching a harness. A contender exits before it can serve state.
The lock's open descriptor is inherited by the native-process supervisor. If the runner dies, that
supervisor terminates and reaps the native harness process group while retaining the descriptor, so
the replacement cannot start until native work is fenced. A child that remains in the group keeps
the inherited lock even after its harness leader exits. See <SPEC.md#durability-and-restart> for
the supported-storage and escaped-process boundary.

Keep `journal.sqlite` and any recovery journal together on the surviving state volume. The checked-in
staging/testing templates mount `/state` from `local-path-ovh-hdd` PVCs. Network filesystems
and deleting the only state volume are not supported recovery paths. That node-local `ReadWriteOnce`
mount is the required ownership assumption; RWO alone does not prevent two processes on its node
from opening it. Session metadata and native resume files remain beside the database. There is no
JSONL reader or old-data migration.

### Bounded session history

The SQLite journal stores a recovery checkpoint in the same transaction as each event.
Opening a session reads that checkpoint and validates the last event, without decoding
historical frames. Attachments query exclusive-cursor pages of 128 events on the retained journal
connection under its transaction lock, limited to the published cursor. A cancelled attachment
waits for its read session to close before releasing that lock to a native callback. A slow
attachment retains one page.
Completed command IDs and debug checkpoint identities are looked up by their indexed
keys rather than retained in process-lifetime sets. Outstanding commands remain in memory.

This changes the disposable journal schema: recreate old staging runner state rather than
replaying it into a compatibility checkpoint. The native harness's own history and memory
usage are separate from the runner journal's bounds.

Historical adapter item identities and Claude per-message block counts use connection-local
SQLite temporary tables with `temp_store=FILE` and a 2 MiB temporary page cache. Each adapter
instance has its own scope, preserving reset-on-new-harness behavior. These lookup tables are
scratch state: they disappear when the journal connection closes and are not recovery evidence.
The current Claude message's block map and unconfirmed inputs remain in memory.

The Python `RunnerClient` tracks its consumed cursor without retaining every received event.
Tests that inspect an attachment's complete `seen` history explicitly enable
`capture_history=True`; application clients use the bounded default.
