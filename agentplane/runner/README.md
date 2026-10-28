# Agentplane runner

A statically linked Go executable that runs the native harnesses of its sessions, a Claude Code process per session
and one shared Codex app-server for its Codex sessions, and exposes both through one protocol, over the control stream and Event ingress it dials. Pod Codex tools can run in the standalone `agentplane-executor` companion. The contract is in <SPEC.md>; the wire definition is
[`agentplane/proto/runner.proto`](../proto/runner.proto).

The deployment renders `platform_instructions.j2` with concrete service URLs; the runner appends
the Sandbox/Session notification destination and caller's assignment. Keep the standing prompt to
platform orientation and essential constraints. Detailed workflows live behind the independent
services' `/llms.txt` indexes, so every Sandbox runtime can discover the deployed guides on demand.

```sh
bbr test //agentplane/runner/...
```

While its ingress (the History Service) is down or rolling, the runner keeps its harnesses
running and admitting commands from the control stream, and keeps every journal entry on its state
volume. Each Session's push retries with the control stream's backoff and resumes after the last
acknowledged entry ([SPEC](SPEC.md#event-ingress)), so the outage delays history but loses none of it.

## Layout

- `lifecycle_runtime.go`, `lifecycle_resources.go`, and `lifecycle_inputs.go`: COSI runtime wiring,
  typed resources, and host input publication ([ownership](docs/lifecycle.md)).
- `runner_controller.go`, `session_controller.go`, `codex_exec_server_controller.go`, `codex_app_server_controller.go`,
  and `bootstrap_controller.go`: one lifecycle controller per file.
- `process_controller.go` and `process_host.go`: COSI process intents, native lifetime observations
  and finalizer-protected cleanup. Codex exec-server uses this shared controller for launch and stop.
- `operations.go`: owned asynchronous protocol/setup operations and completion routing.
- `bootstrap.go`: durable bootstrap selection and the one-shot script effect.
- `runner.go`: Runner construction, durable inventory, incarnation adoption and lifetime.
- `admission.go` and `status.go`: public admission and status/facts RPCs.
- `session.go`: Session host state, readiness and recovery; `session_events.go` owns journal-backed
  Event effects, and `session_native.go` owns native process, dispatch and stream effects.
- `session_setup.go`: the accepted one-shot Session setup effect.
- `codex_exec_server.go`: isolated Codex execution operations, RPC probes and container ownership fencing
  ([execution modes](docs/codex_executor.md)).
- `executor/`: the PID-1 companion for Pod Codex execution, its Unix control protocol and reaper.
  `executor/cmd/` builds the standalone `agentplane-executor` binary.
- `codex_app_server.go`: the Runner's one Codex app-server, its startup and exit, and the journaling of
  each frame in the Sessions that `codex/router.go` assigns it to.
- `transport.go`: the outbound control tunnel and independent per-Session ingress
  pushes, including heartbeat, replay, acknowledgement and backpressure.
- `journal.go`, `native_requests.go`, `journal_migrations/` and `store.go`: the Goose-managed SQLite
  schema, verbatim protobuf Event blobs, native request records with foreign-key links
  to their originating commands, session metadata, ingress acknowledgements and ownership lock.
  Transactions use `database/sql` with the cgo-free SQLite driver. Outcomes and all
  coalesced command receipts commit together; publication and startup handshake replies follow commit.
- `supervision/`: the shared native-process API and its private systemd/subprocess backends.
  Runner prepares supervision while holding state ownership; controllers launch through the
  selected supervisor. The package owns streams, exit observation and execution-domain cleanup.
  `systemd/` contains the static guest units. See [supervision](docs/supervision.md).
- `claude/` and `codex/`: harness-specific launch, frame translation and native
  history readers. They depend on the shared adapter contract and recovery types in
  `harness/`, independently of the runner runtime. Unknown frames remain in raw history,
  and derived Events retain their sources.
  Native argument payloads remain as received; runner-constructed projections use standard
  JSON encoding rather than reproducing the former Python runner's formatting.
- `config.go`, `guest.go` and `cli.go`: explicit child environment and launch
  configuration. `cmd/main.go` dispatches internal process modes before service startup.
  Pods and VMs invoke the same executable with explicit `--config` and `--launch-file` arguments. The [Runner configuration inventory](CONFIGURATION.md)
  documents the public flags, configuration sources and VM paths for Pod and VM deployments.

The production build targets are `//agentplane/runner/cmd:agentplane-runner` and
`//agentplane/runner/executor/cmd:agentplane-executor`, shipped together in
`//agentplane/runner:disk_image`. The runner includes its supervisor and SQLite; the companion is
independent of the Runner runtime and SQLite. The native Claude/Codex executables, workspace tools
and CA certificates are supplied by the runtime image.

## Codex executor container

Runner Pods can run Codex command execution through a companion container in the same Pod. The runner
retains the Codex app-server and journals; the companion runs as PID 1 and supervises
`codex exec-server` in its private container PID namespace. The runner claims the companion over a Unix socket before Codex
starts and fences it before replacing a lost process. A companion exit or failed health probe lets
Kubernetes restart that container with a fresh PID namespace; the runner never replays an in-flight
command. `Environment.isolated_codex_executor` controls this Pod-template policy and can be disabled
for environments that need execution inside the runner container.

Both containers run as the image's uid and gid 1000 and share the Sandbox state and workspace at the
same absolute paths, plus `/home/runner`, `/tmp`, and the read-only runner disk image. They use the
same proxy and CA settings. Processes in either container have the same filesystem identity and can
change the shared files, so the companion is a process and resource boundary, not a trust boundary.
Its Kubernetes requests and limits are independently configurable with `executor_resources` on
`RunnerTemplate`; runner resource overrides continue to apply only to the runner container. The
companion receives the Sandbox kubeconfig and any optional BuildBuddy diagnostics key so its command
tools keep the runner's intended access. It gets no model credentials or sidecar-projected tokens.

`codex/protocol` generates Go models at build time with Quicktype from schemas
exported by the same pinned Codex executable used by the native tests. Requests,
responses, notifications and known item payloads use these models. Method and item
dispatch keep a raw JSON boundary for unknown variants; recorded native frames are
never reconstructed from decoded models. Codex reconciliation uses turn-scoped
`thread/items/list` RPCs on that connection, with the same generated protocol types; it does not
read Codex's private files or databases. Schema export and JavaScript code generation are
build dependencies only.

Claude stream events and assistant content blocks use the Anthropic Go SDK's
discriminated unions. Consumed variants are checked with strict JSON decoding
because the SDK's response decoder tolerates malformed fields. Tool inputs use
the SDK's raw-field metadata to preserve whitespace, numeric tokens and explicit
`null`. Claude Code control packets use the standard-library-only
`claude-agent-sdk-go/types` envelopes and matching subtype structs. Local
extensions cover `appendSystemPrompt`, interrupt metadata, model/effort changes,
raw tool inputs and explicit null reply fields. User tool results have their own
wire type.

The integration fixtures launch this executable and drive its public control and ingress
protocols. `test_turns.py`, `test_subagents.py`, and `test_restart.py` exercise the real
native harnesses; App, History, Gateway, and Notification integration tests use the
same Go process.

## Release and image inputs

Bazel builds the runner disk image (`//agentplane/runner:disk_image`) from source at the same commit
as the runner. The VM guest does not embed the runner: the image packs it, the executor companion
and the `agentplane` command into a read-only erofs (`disk/runner.img`) that the `runner-vm` template
attaches beside the guest; the same image also carries them as plain files under `bin/`, which the
runner Pod templates mount at `/run/agentplane/runner` and start the runner and companion from, so
the Pod's tools image (`image.nix`) holds neither. The k3s fixture mounts the same image. A runner,
companion or CLI change therefore republishes the runner disk image but not the `runner` tools
image. The `agentplane-cli` artifact pin still feeds the Action sandbox images, which rebuild on
any pin bump.

## VM workspace disk

The guest declares `/state/workspaces` in NixOS `fileSystems` as ext4 with `autoFormat`:
`systemd-makefs` creates the filesystem when `blkid` finds no signature and skips any disk that
has one, so a disk holding data but no recognized signature is formatted too
([why that is accepted](../docs/vm_sandboxes.md#guest-and-storage)).

`systemd.tmpfiles` owns the mounted workspace directory's `agent:agent` ownership, setgid mode,
default ACL and private `.home` directory.

## Tests

`//agentplane/runner:runner_test` and `//agentplane/runner/codex:codex_test` run with Go's race detector. The
Codex router test drives parallel sessions through shared host, request, and thread routing state. The
runner suite includes its journal, process, and Session tests. The VM fixture runs the separate
pure-Go `//agentplane/runner/supervision:runner_systemd_fixture` test binary against its real systemd manager before starting
Runner. It exercises streams and credentials, resource mapping, lost launch replies, Runner death,
detached descendants and scoped startup fencing; VM system tests exercise actual tools and OOM.
The production `//agentplane/runner/cmd:agentplane-runner` remains pure-Go.

### Native subagents

Both launches enable native delegation: Codex multi-agent tools and Claude's `Agent`,
`SendMessage`, and `TaskOutput`. Children remain native harness conversations, not additional
Agentplane Sessions. Discovery, attachment, independent child controls, and child transcript UI
are not provided.

All received native frames remain in the Session's raw event history. Normalized transcript and
turn state belong only to the root conversation: Codex notifications are scoped by root thread ID;
Claude child-attributed frames and task lifecycle notices are raw-only. Control requests and RPC
responses still receive normal handling, without child output acknowledging pending root inputs.
Root delegation calls and results remain visible, including Codex child-state summaries.

Codex v2 `subAgentActivity` maps to an existing tool item. Its successful result means the reported
activity was observed (including an interruption), not that the child succeeded. The result retains
the native activity kind and child identity; unrecognized future activity kinds remain raw-only.

`test_subagents.py` drives both real harnesses with mock model responses, checking background-child
isolation while the parent is idle (Claude) or active (Codex), with only one Agentplane Session.

### Test infrastructure

Each test is one interaction script run against both harnesses, written against
`testing/driver.py`: it plays the Runner Gateway's side of the control stream and reads each
session's Events from the stand-in ingress in `testing/gateway.py`;
the parametrized `model` fixture is the only place that knows the model API dialect. The scripted
model is shared with other packages' tests in [`agentplane/testing/`](../testing):
`scripted_model.py` is the neutral vocabulary (`Text`, `Reasoning`, `ShellCall`, and the request
markers), and `claude_model.py` and `codex_model.py` speak the two dialects. Harness fixtures live
in `testing/`, where `launches.py` wires the pinned binaries to a scripted upstream. `test_restart.py`
runs the runner as its own process so a crash takes its harnesses with it. No test here runs the
image (<image.nix>), which carries nixpkgs' harnesses rather than these pinned ones; its header
says what to run after a bump.

`persistence_test.go` checks retained-state compatibility, command deduplication,
transaction publication, coalesced receipts, rollback, and seals. `session_test.go`
checks native reply publication after translation commits and final frames at EOF.
`transport_test.go` exercises real loopback gRPC replay, durable acknowledgements,
the ingress window, and seal ordering. `process_test.go` checks parent-death fencing,
native descendants, inherited state ownership, and cancellation during blocked input.
These are not physical power-loss tests.

## Native continuation evidence

`recovery.go` reconstructs observed item content from the journal's indexed turn boundaries.
The adapters compare it with native continuation evidence and emit `ConversationReconciled`;
the app only folds that neutral report.

- `claude/history.go` walks the persisted main parent chain, correlates split assistant blocks,
  and removes what Claude's resume loader removes: messages with an unresolved tool call, then a
  message holding only thinking once no other entry of its message id is left, so thinking
  before a tool that never answered is not replayed even though the transcript holds it. Ordinary
  interrupts use the native terminal item observations, and the same rule for thinking from the
  turn's journaled assistant frames: a completed thinking block is retained only if another block
  of its message was written, and thinking that would otherwise be absent is unknown when a native
  line of the turn could not be parsed. Compacted or missing history and unfinished live tool outcomes
  remain unknown. The reader is tested against Claude Code 2.1.252.
- `codex/history.go` verifies paginated history and reads `thread/items/list` through the shared
  app-server connection. It fetches only selected recovery turns and the original owning turns of
  items that finish later, follows opaque cursors with limit 100, and shares one snapshot across
  a resume. A turn without observations can still carry a compaction barrier for an earlier item;
  an entirely empty recovery window needs no RPCs. Assistant text, visible reasoning summaries and
  fully observed completed command output are compared by native item ID. Legacy/unsupported
  history, incomplete page sequences, compaction barriers and uncertain tool context stay unknown.
  There is no private Codex file/database reader or synthetic tool-output reconstruction. The
  [protocol characterization](../debug/codex_app_server_history_apis.md) records what the pinned
  0.157.0 API exposes and the real runner/model tests that support the comparison.

Unreadable, malformed, or unsupported native evidence produces an explicit unknown report.
These adapters depend on the pinned native protocols and must be checked with the app-level recovery
matrix when upgrading a harness. The matrix covers interrupt, graceful shutdown/resume, and
process-group kill/resume at three gates: streaming text, tool execution, and streaming after a
completed tool. The mock reads tool results throughout each request, including older user messages.
Both harnesses retain the tested completed results.

## SQLite storage

The connection uses `journal_mode=DELETE` and `synchronous=EXTRA`; publication relies on SQLite's
[durable rollback-journal commits](https://sqlite.org/pragma.html#pragma_synchronous) and storage honoring sync.
A journal mutex owns each complete transaction, including batch publication.
Explicit `BEGIN IMMEDIATE` reserves the writer before reading ids/cursors; no transaction spans
harness or network I/O. One retained connection serves the single runner-session writer.
That SQLite transaction is not the native-execution fence: a runner owns its whole retained state
directory through a nonblocking lifetime lock on the fixed `.agentplane-runner-owner` inode before
opening a journal, dialing out, or launching a harness. A contender exits before it can serve state.
In Pods the lock descriptor is inherited by the native-process supervisor, which retains it through
process-group cleanup after Runner death. In VMs systemd owns native cgroups; a replacement acquires
the lock and then fences prior owned services before opening Session stores or journals.
See <SPEC.md#durability-and-restart> for storage, startup barriers and containment boundaries.

The stdout reader commits in groups (`Journal.WithBatch`): the lines one pipe read delivered and the
Events derived from them share a transaction, so a burst of output costs one commit rather than one
per Event. It commits early before writing to the harness, so the record precedes the write, and
before waiting on the session lock.

The adapter's `OnFrame`, called by the reader in frame order, records every Event derived from the
harness's output, and each such Event names its Native sources. A command whose effect a reply
proves (Codex's `turn/start`, Claude's `set_model`) keeps what the reply means under the request's
native id before sending it, and `OnFrame` records the effect when the reply arrives. The requester
still waits for the reply, only to dispatch normal commands one at a time; it gets the reply once
the batch recording it and its translation commits.

Keep `journal.sqlite` and any recovery journal together on the surviving state volume. The checked-in
staging/testing templates mount `/state` from `piraeus-hdd-local` PVCs. Network filesystems
and deleting the only state volume are not supported recovery paths. That node-local `ReadWriteOnce`
mount is the required ownership assumption; RWO alone does not prevent two processes on its node
from opening it. Session metadata and native resume files remain beside the database.

### Bounded session history

The SQLite journal stores a recovery checkpoint in the same transaction as each event.
Opening a session reads that checkpoint and validates the last event, without decoding
historical frames. Journal follows, such as a session's ingress push, query exclusive-cursor pages of
128 events on the retained journal connection under its transaction lock, limited to the published
cursor. Reads close their SQL rows before releasing that lock to a native callback. A slow
follow retains one page.
Completed command IDs and debug checkpoint identities are looked up by their indexed
keys rather than retained in process-lifetime sets. Outstanding commands remain in memory.

The native harness's own history and memory usage are separate from the runner journal's bounds.

Historical adapter item identities and Claude per-message block counts use connection-local
SQLite temporary tables with `temp_store=FILE` and a 2 MiB temporary page cache. Each adapter
instance has its own scope, preserving reset-on-new-harness behavior. These lookup tables are
scratch state: they disappear when the journal connection closes and are not recovery evidence.
The current Claude message's block map and unconfirmed inputs remain in memory.

## Ducktape development image

`nix build .#runner-ducktape-image` builds the `runner-ducktape` container every staging Pod Sandbox
runs. It reuses this runner, both harnesses, and the sandbox substrate, adding `preCommitPackages`
from the shared flake tool definitions and the repo-configured Gazelle. This includes `bb`, `bbr`,
Bazelisk, pre-commit and the pinned formatters without duplicating their package/version list.
Tools arrive without changing the runner executable or the internal service-account and
network-policy identities, so it stays the environment's one Pod execution environment.

`nix build .#checks.x86_64-linux.runner-ducktape-tools` exercises representative repository
hooks in a sandboxed Nix derivation, using the exact tool closure included in the image. The
image workflow runs that check and a thin non-root container smoke test for the assembled
filesystem, entrypoint and tool availability. These prove local tooling, not authenticated BuildBuddy execution;
a deployed sandbox must separately prove the normal proxy/CA/credential route for `bbr`.
No credentials are embedded in the image. The staging container workflow is BuildBuddy-hosted
builds, not a local Bazel client with remote actions. Local Bazel execution is deferred until
VM-backed environments; package availability does not imply permission to run it. Adding tools
does not itself establish a tool memory boundary. Isolated Codex execution is configured separately
([executor lifecycle and scope](docs/codex_executor.md)). See the
[BuildBuddy execution policy and validation](../docs/buildbuddy_remote_auth.md#validation-and-execution-policy).
