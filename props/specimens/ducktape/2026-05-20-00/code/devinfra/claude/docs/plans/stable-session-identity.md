# Plan: Stable Session Identity for Hook State

**Status**: Draft
**Created**: 2026-03-30

## Problem

Claude Code assigns a new internal session UUID on every resume, compact,
and clear. Hook daemons key all state (proxy sockets, bazelrc, env files)
on this UUID. When the UUID changes:

1. Bazel server's `--remote_proxy=unix:<old-uuid-path>` breaks
2. Bazel wrapper's RPC target changes → Bazel detects changed startup
   option → kills server → 45-min cold Skyframe reload
3. No clean handoff mechanism — `SessionStart` hook receives the new UUID
   but has no reference to the old one

## Goal

Long-lived processes (Bazel server, Docker daemon) survive session
transitions without restart. The hook daemon's state should be keyed on
a stable identity, not the ephemeral session UUID.

## Stable Identity Candidates

| Candidate                            | Stable across   | Available in  | Notes                                                                  |
| ------------------------------------ | --------------- | ------------- | ---------------------------------------------------------------------- |
| `CLAUDE_CODE_SESSION_ID` (`cse_...`) | resume, compact | Web mode only | API-level session ID. Not set in CLI mode.                             |
| `CLAUDE_PROJECT_DIR`                 | all transitions | Both modes    | Workspace path. Two concurrent CLI agents on same project would clash. |
| Hash of project dir                  | all transitions | Both modes    | Avoids path length issues. Two CLI agents still clash.                 |
| `CLAUDE_CODE_SESSION_ID` + PID       | resume, compact | Web mode      | Unique per agent process. But PID changes on resume.                   |

### Decision: `CLAUDE_CODE_SESSION_ID` for web mode

On web, `CLAUDE_CODE_SESSION_ID` is the stable identity. It survives
resume and compact (confirmed by traces — same `cse_01ANqo...` across
sessions `1c9fe809` → `3a1f5b5a`). Each web session is one logical
agent, so no clash risk.

### Open question: CLI mode

CLI mode doesn't have `CLAUDE_CODE_SESSION_ID`. Options:

1. **Project dir hash**: simple, but two `claude` processes on the same
   project would share state. Might actually be fine — they'd share the
   Bazel server anyway (same output base).
2. **Project dir hash + some disambiguator**: avoids clash but what's
   stable? PID changes on resume.
3. **Don't use stable paths in CLI mode**: CLI mode doesn't have the
   auth proxy / TLS proxy problem. The bazel wrapper in CLI mode just
   passes through to bazelisk without proxy cred refresh. So the session
   UUID change doesn't break anything — there's no UDS proxy socket to
   invalidate.

**Tentative**: CLI mode can keep current behavior (UUID-keyed paths).
The proxy/bazelrc stability problem is web-only. Two CLI agents sharing
a project dir already share the Bazel server (same output base), so
there's no new clash.

## Proposed Changes (Web Mode)

### 1. Stable proxy socket path

**Current**: `/tmp/claude-hd/<session-uuid>/remote-proxy.sock`
**Proposed**: `/tmp/claude-hd/<CLAUDE_CODE_SESSION_ID>/remote-proxy.sock`

The daemon creates the proxy at this stable path on every SessionStart.
On resume, the new daemon takes over the same socket path. The Bazel
server's `--remote_proxy` points here and never changes.

### 2. Stable bazelrc path

**Current**: `~/.claude/session-env/<session-uuid>/bazelrc`
**Proposed**: `~/.claude/session-env/<CLAUDE_CODE_SESSION_ID>/bazelrc`
(or a symlink from a stable path to the current session's bazelrc)

The bazel wrapper injects `--bazelrc=<path>`. If the path doesn't
change, Bazel doesn't detect a startup option change and keeps the
existing server.

### 3. Session-env directory structure

**Current**: one directory per internal UUID

```
~/.claude/session-env/
  1c9fe809-b4dd-4f3f-924c-900b1ebfeaad/  (dead after resume)
  3a1f5b5a-0438-416c-b26d-ddcb3e5a4dad/  (current)
```

**Proposed**: stable directory keyed on `CLAUDE_CODE_SESSION_ID`, with
per-UUID subdirs for things that genuinely need per-transition state

```
~/.claude/session-env/
  cse_01ANqoTWWCxF71H5Aq2DqwnT/         (stable, survives resume)
    bazelrc
    remote-proxy.sock → /tmp/claude-hd/cse_.../remote-proxy.sock
    auth-proxy/
    bin/                                  (bazel wrapper)
    transitions/
      1c9fe809/                           (historical, can be cleaned up)
      3a1f5b5a/                           (current transition's logs, etc.)
```

### 4. Companion process detection

Companion processes (instruction pre-loading) are detected by the
absence of `CLAUDE_ENV_FILE` in the hook env. When this is missing,
the daemon should skip SessionStart setup entirely — no proxy, no
bazelrc, no supervisor.

## What the sandbox preserves

Observed: `/tmp/claude-0/-home-user-ducktape/` persists across resume.
It contains per-UUID subdirectories for sandbox task outputs, but the
parent directory survives. This suggests Anthropic's sandbox preserves
the filesystem across compaction/resume within the same VM.

The session env dir (`~/.claude/session-env/`) also persists — old
session dirs from previous UUIDs remain on disk after resume.

## How Community Projects Handle This

**No one has a clean pattern yet.** Research (2026-03-30) across major
Claude Code hooks projects:

| Project                                                                                  | Approach                              | Identity key                                                     | Limitation                                                                 |
| ---------------------------------------------------------------------------------------- | ------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------- |
| [claude-mem](https://github.com/thedotmack/claude-mem) (~43k stars)                      | SQLite + Express worker on port 37777 | Per-project DB                                                   | Race conditions on worker startup; stale `memory_session_id` after restart |
| [Continuous-Claude](https://github.com/parcadei/Continuous-Claude-v3)                    | PostgreSQL + YAML handoff files       | File claim ownership                                             | "Compound, don't compact" philosophy — avoids the problem                  |
| [ClaudeFast](https://claudefa.st/blog/tools/hooks/context-recovery-hook)                 | Markdown backups at token thresholds  | Shared state file (`~/.claude/claudefast-statusline-state.json`) | Manual reload after compaction                                             |
| [claude-qmd-sessions](https://github.com/wbelk/claude-qmd-sessions)                      | QMD format conversion                 | Project directory                                                | PreCompact/SessionStart stdout injection                                   |
| [MCP Memory Service](https://crunchtools.com/how-to-give-claude-code-persistent-memory/) | systemd + SQLite-vec, SSE transport   | Per-project DB                                                   | SSE avoids stdio startup race but adds complexity                          |

**Key insight from claude-mem**: Their daemon had the same spawn-race
we observed — hooks fire before the worker is ready. Fixed with
`shutdownInitiated` flags and regression tests. Stale session IDs in
the DB caused crashes after worker restart.

**Key insight from MCP Memory Service**: `SessionStart` hooks fire
before stdio MCP servers initialize, so they use SSE (long-lived
service) instead. This is analogous to our daemon approach.

**Nobody uses `CLAUDE_CODE_SESSION_ID` as a stable key** — it's
undocumented for this purpose. Our approach of keying on it for web
mode would be novel.

### `/tmp/claude-*` sandbox paths

- `/tmp/claude-{4hex}-cwd` files: per-Bash-invocation CWD tracking,
  [never deleted](https://github.com/anthropics/claude-code/issues/8856)
  (confirmed bug, accumulates ~174/day)
- `/tmp/claude-0/-home-user-ducktape/` persists across resume (observed)
  but is not a reliable identity mechanism
- `CLAUDE_CODE_TMPDIR` has a sandbox
  [mismatch bug](https://github.com/anthropics/claude-code/issues/21842)

### References

- [Memory leak: /tmp/claude-\*-cwd files (Issue #8856)](https://github.com/anthropics/claude-code/issues/8856)
- [Feature Request: Persistent Memory (Issue #14227)](https://github.com/anthropics/claude-code/issues/14227)
- [Race condition in storage path (Issue #24125)](https://github.com/anthropics/claude-code/issues/24125)

## Implementation Order

1. **Make `CLAUDE_CODE_SESSION_ID` available to SessionPaths** — thread
   it from CallerContext through to path computation
2. **Stable proxy socket path** — change daemon to use
   `CLAUDE_CODE_SESSION_ID` as the socket directory key in web mode
3. **Stable bazelrc** — change path generation to use stable key
4. **Bazel wrapper** — update `DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR` to
   point to stable dir (or make wrapper resolve it)
5. **Cleanup** — garbage-collect old per-UUID dirs that aren't the
   current stable dir
6. **Companion skip** — check for `CLAUDE_ENV_FILE` absence early in
   daemon startup, skip all setup if missing

## Risks

- **`CLAUDE_CODE_SESSION_ID` format changes**: If Anthropic changes the
  format of the `cse_...` ID, paths break. Mitigated by hashing it.
- **Concurrent sessions on web**: Not expected (one session per
  container), but if it happens, `CLAUDE_CODE_SESSION_ID` would clash.
  This is the same risk as the current design (one daemon per UUID).
- **CLI mode regression**: Keeping UUID-keyed paths means CLI behavior
  is unchanged. No new risk.
