---
description: Create a session tombstone capturing work done, open threads, and context for future sessions
---

Capture the current session state as a persistent markdown tombstone document.

## Purpose

Session continuity beyond Claude Code's built-in session restoration:
- Resuming work after restart/closure
- Handoff to future agents
- Historical record of incomplete work

**Different from /followups:** This captures "what did we discuss, what's not finished" — not speculative "what could you do next".

## Scope

| Mode | Invocation | Use Case |
|------|------------|----------|
| Full session | `dump` | Save everything before closing |
| Scoped | `dump <topic>` | Extract specific discussion thread |

## Required Elements

### Session Metadata

- Session ID and file path (use session-logs skill to verify)
- Date, working directory, git branch

### What Was Done

- Files created/modified with file:line references
- Commands run, tests/builds results
- Commits made
- Key URLs, commit SHAs, error messages encountered

**Keep references immutable:** `foo.py:123`, full commit SHAs, URLs with anchors — not "the function we edited".

### Incomplete Work (CRITICAL)

This is the highest priority section. Scan conversation for:

| Source | Examples |
|--------|----------|
| Explicit statements | "we should...", "let's do X next", "TODO" |
| Unresolved issues | Bugs discovered but not fixed, failing tests |
| Partial work | WIP states, uncommitted changes, partial implementations |

**Distinguish:** Actual discussed items (high priority) vs. potential next actions (lower priority).

### Context for Successor

- Pointers to project docs (`@AGENTS.md`, `@README.md`)
- Build/test commands to verify work
- Key decisions/constraints from this session

Keep concise — point to existing docs rather than repeating.

## File Placement

1. If recent dump with similar scope exists → update it
2. If project has `docs/` → place there
3. If working on specific component → place nearby
4. Otherwise → project root

**Naming:** Short topic summary, 2-4 words, lowercase-with-hyphens (e.g., `bundle-refactor.md`). No dates unless multiple dumps of same topic.

## Implementation Notes

**Session analysis via session-logs skill:**
- Tool calls (Edit, Write, Bash) for modified files
- User messages for "should", "TODO", "next" patterns
- Timestamps for session duration

**Update vs. new file criteria:**
- Same general topic, recent (~1 week), not too large (<1000 lines) → update
- Otherwise → create new

**Verification:**
- Session ID must be verified via skill
- File paths must exist
- Line numbers current (best effort)
