# Harness Host retained state and test evidence

Harness Host owns Session journals, recovery checkpoints, native continuation evidence, and History
ingress delivery. The Sandbox state volume is durable user data and remains at its existing paths.

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
  [protocol characterization](../../../../debug/codex_app_server_history_apis.md) records what the pinned
  0.157.0 API exposes and the real Harness Host/model tests that support the comparison.

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
harness or network I/O. One retained connection serves the single Harness Host-session writer.
Runtime acquires the legacy environment lifetime lock before starting Host. Host acquires its distinct
`harness-host/.agentplane-harness-host-owner` lock before opening Session journals or launching natives;
a contender exits before it can serve state. In production Pods Runtime is PID 1 and supervises Host,
which supervises native subprocesses. The outer container runtime fences escaped descendants before
an abnormal replacement. In VMs systemd owns each component's distinct scope; startup fences prior
owned services before opening the Session stores or journals.
See <../SPEC.md#durability-and-restart> for storage, startup barriers and containment boundaries.

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

The native harness's own history and memory usage are separate from the Harness Host journal's bounds.

Historical adapter item identities and Claude per-message block counts use connection-local
SQLite temporary tables with `temp_store=FILE` and a 2 MiB temporary page cache. Each adapter
instance has its own scope, preserving reset-on-new-harness behavior. These lookup tables are
scratch state: they disappear when the journal connection closes and are not recovery evidence.
The current Claude message's block map and unconfirmed inputs remain in memory.
