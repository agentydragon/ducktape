# Haku bridge v3 frame log

The v3 cutover makes `session_frames` a forensic log of the bridge, not a projection of one
harness vocabulary.

- `kind` is only `harness_frame` or `setup_output`.
- `payload` is exactly the native harness JSON object. Claude `type` values, Codex JSON-RPC
  methods, and unknown fields are preserved and are not copied into the outer `kind`.
- `direction`, `created_at`, `updated_at`, and the runner's `runner_seq` are retained for replay and
  ordering analysis.
- Reconnect deduplication is positional (`session_id`, `runner_seq`), so native deltas and
  notifications are retained/replayed exactly like every other harness frame. There is no
  backend-specific `replayable()` classifier in the runner.

This release was intentionally incompatible and negotiated version 3 only. The later v4 MCP
credential-boundary cutover retains v3 for old Consoles while requiring v4 for session-bearer MCP
launches. A runner that advertises no acceptable version is refused and its session is allowed to
terminate/clean up; it must not be guessed into the v2 envelope. Migration
`0090_harness_frame_log_cutover` clears session and
chat-derived rows while preserving Operators, credentials, approvals, provider connections,
conversations, and Matrix room attachments. The Matrix supervisor creates replacement sessions
against the preserved conversation/room association.

The frame inspector derives the native frame type as a presentation detail, while filtering and
storage continue to use the bridge class. Exports include `bridge_kind` and `wire_seq` alongside the
redacted, otherwise unchanged native `frame`.
