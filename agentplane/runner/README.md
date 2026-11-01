# Agentplane runner

The Runner wire contract is served by three process owners:

- **Environment Runtime** (`agentplane/env/runtime`) owns Sandbox identity, configuration,
  bootstrap, Gateway discovery/control, health and coordinated drain.
- **Harness Host** (`agentplane/env/harnesshost`) owns native Session admission, one Claude
  process per Session, the shared Codex app-server, continuation state, journals and History ingress.
- **Execution Service** (`agentplane/env/execution`) owns typed filesystem/process execution
  and the isolated native executor. Central sampler remains outside `env/`.

The public contract is in <SPEC.md>; the wire definition remains
[`agentplane/proto/runner.proto`](../proto/runner.proto). Runtime forwards native Session calls to
Host over a private Unix socket with a Runtime boot, Host boot and admission generation binding.

The deployment renders `platform_instructions.j2` with concrete service URLs; the runner appends
the Sandbox/Session notification destination and caller's assignment. Keep the standing prompt to
platform orientation and essential constraints. Detailed workflows live behind the independent
services' `/llms.txt` indexes, so every Sandbox runtime can discover the deployed guides on demand.

```sh
bbr test //agentplane/env/... //agentplane/runner/...
```

While its ingress (the History Service) is down or rolling, the runner keeps its harnesses
running and admitting commands through the configured control transport, and keeps every journal
entry on its state volume. Each Session's push retries with ingress backoff and resumes after the
last acknowledged entry ([SPEC](SPEC.md#event-ingress)), so the outage delays history but loses none of it.

## Layout and entrypoints

| Owner               | Executable target                                             | Main packages                                                                  |
| ------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Environment Runtime | `//agentplane/env/runtime/cmd:agentplane-environment-runtime` | environment owner/state, private lifecycle protocol, bootstrap, public control |
| Harness Host        | `//agentplane/env/harnesshost/cmd:agentplane-harness-host`    | Session, Claude/Codex adapters, app-server, retained records, journal, ingress |
| Execution Service   | `//agentplane/env/execution/cmd:agentplane-execution-service` | execution adapter, native exec-server, companion                               |

Shared process machinery is in `env/supervision`; one-shot operation bookkeeping is in
`env/oneoff`, shell execution in `env/scripts`, and Go deployment parsing in `env/configuration`.
Host owns workspace containment and helpers in `env/harnesshost/workspace`. The existing `runner/` directory retains wire contracts, deployment assets and cross-process integration tests.

Runtime and Host read the same `--config` and `--launch-file`. Runtime launches the Host with
private lifecycle and Session socket paths and an explicit `--supervision subprocess|systemd`.
Production Pod Runtime is PID1; its Host child uses subprocess supervision. VMs use distinct Runtime,
Host and Execution systemd scopes. Runtime retains the original environment owner inode; Host locks
`<state_dir>/harness-host/.agentplane-runner-owner`. Session state stays under `<state_dir>/sessions`
and the shared Codex home stays under `<state_dir>/codex`.

The private lifecycle protocol binds every intent/report to the same process boots and generation.
Host drains admitted work and native processes before reporting `native_stopped`. Runtime fences
Execution before acknowledging `execution_fenced`; only then may Host seal journals and report exact
History acknowledgements. Losing the control stream closes native admission and drains Host without
creating a seal. An abnormal Pod Host exit requires the outer container fence before replacement.
See [Host contract](../env/harnesshost/SPEC.md) and [lifecycle](docs/lifecycle.md).

The three executables ship with the `agentplane` CLI in `//agentplane/runner:disk_image`.
Native Claude/Codex executables, workspace tools and CA certificates come from the runtime image.

## Codex executor container

Runner Pods can run Codex command execution through a companion container in the same Pod. The runner
retains the Codex app-server and journals; `agentplane-execution-service companion` runs as PID 1 and supervises
`codex exec-server` in its private container PID namespace. The Execution Service claims the companion over a Unix socket before Codex
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

`../env/harnesshost/codex/protocol` generates Go models at build time with Quicktype from schemas
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

The integration fixtures launch Runtime and its separate Host/Execution processes and drive its public control and ingress
protocols. `test_turns.py`, `test_subagents.py`, and `test_restart.py` exercise the real
native harnesses; App, History, Gateway, and Notification integration tests use the
same process topology.

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

`//agentplane/env/harnesshost:harnesshost_test` and `//agentplane/env/harnesshost/codex:codex_test` run with Go's race detector. The
Codex router test drives parallel sessions through shared host, request, and thread routing state. The
journal suite (`//agentplane/env/harnesshost/journal:journal_test`) also runs with the race detector;
the runner suite covers process and Session integration. The VM fixture runs the separate
pure-Go `//agentplane/env/supervision:runner_systemd_fixture` test binary against its real systemd manager before starting
Runner. It exercises direct argv/stream semantics, lost launch replies, Runner death, stop status,
reaper retries and scoped startup fencing. Ordinary VM Session setup checks workspace creation,
credentials, resource mapping and detached descendants; VM tools cover execution and OOM.
The production `//agentplane/env/harnesshost/cmd:agentplane-harness-host` remains pure-Go.

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

`journal/journal_test.go` checks retained journal compatibility, command deduplication,
transaction publication, coalesced receipts, rollback, and seals. `persistence_test.go` checks
session metadata and initialization log compatibility. `session_test.go`
checks native reply publication after translation commits and final frames at EOF.
`ingress/client_test.go` exercises real loopback gRPC replay, durable acknowledgements,
journal wakeups, backpressure and seal ordering; `ingress/supervisor_test.go` covers per-Session
worker discovery and joins. `transport_test.go` covers Runner wiring, COSI acknowledgement
observation and shutdown flushing. `process_test.go` checks parent-death fencing,
native descendants, inherited state ownership, and cancellation during blocked input.
These are not physical power-loss tests.

## Native continuation evidence

`journal/recovery.go` reconstructs observed item content from the journal's indexed turn boundaries.
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
Runtime acquires the legacy environment lifetime lock before starting Host. Host acquires its distinct
`harness-host/.agentplane-runner-owner` lock before opening Session journals or launching natives;
a contender exits before it can serve state. In production Pods Runtime is PID 1 and supervises Host,
which supervises native subprocesses. The outer container runtime fences escaped descendants before
an abnormal replacement. In VMs systemd owns each component's distinct scope; startup fences prior
owned services before opening the Session stores or journals.
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
