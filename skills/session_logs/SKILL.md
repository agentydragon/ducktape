---
name: session_logs
description: Discover and read the complete current Claude Code or Codex CLI conversation, including user turns, nearby agent context, tool activity, and compaction boundaries.
---

# Session transcript recovery

Use this skill when a task depends on what the user said earlier, when `/followups`
needs to find loose threads, or when the in-context conversation may have been
compacted. The harness transcript is the durable source for the conversation.

The procedure is intentionally local and read-only. Find the transcript belonging
to this agent's own harness, then read every human/user turn together with the two
preceding assistant/agent messages. Use the result as conversation context; do not
only inspect the last few turns or trust a compaction summary to preserve the
original problem.

## Paved commands

The packaged skill contains three executable helpers:

```bash
find-current-session.sh [claude|codex]
analyze-session.sh [claude|codex|TRANSCRIPT.jsonl]
conversation.sh [claude|codex] [TRANSCRIPT.jsonl]
```

`conversation.sh` prints every user window in chronological order. Each window
contains the complete user text and up to two preceding assistant messages. It
also prints compaction markers and continues scanning after them. Do not pipe it
through `head`, `tail`, or a truncating pager when doing the recovery pass. For a
large transcript, read the output in sequential chunks and verify the final user
message number.

When running from a checkout rather than an installed package, use
`skills/session_logs` in place of the installed skill directory below.

### Claude Code 2.1.260

Validated on this machine with Claude Code `2.1.260`:

```bash
CLAUDE_SESSION=$(~/.claude/skills/session_logs/find-current-session.sh claude)
~/.claude/skills/session_logs/analyze-session.sh "$CLAUDE_SESSION"
~/.claude/skills/session_logs/conversation.sh claude "$CLAUDE_SESSION"
```

Claude stores project transcripts as JSONL under
`~/.claude/projects/<pwd-with-slashes-replaced-by-dashes>/`. Root session files
are the conversation; `subagents/` files are separate agent conversations and
must not be substituted for the current session. In Claude Code 2.1.260,
human text is in `type: "user"` entries whose content is a string or text block;
`tool_result` entries, task notifications, and system reminders are harness
traffic rather than user turns. Compaction is recorded as
`type: "system", subtype: "compact_boundary"` in the same JSONL file.

### Codex CLI 0.153.4

Validated on this machine with Codex CLI `0.153.4`:

```bash
CODEX_SESSION=$(~/.codex/skills/session_logs/find-current-session.sh codex)
~/.codex/skills/session_logs/analyze-session.sh "$CODEX_SESSION"
~/.codex/skills/session_logs/conversation.sh codex "$CODEX_SESSION"
```

Codex stores transcripts as
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`. The current CLI exports
`CODEX_THREAD_ID` (also accepted: `CODEX_SESSION_ID`); the canonical parent
filename ends in that id. Approval flows can create a smaller paired JSONL file
with the same session id, so the helper selects the larger parent transcript.
Codex user turns are `response_item` records with
`payload.type: "message", payload.role: "user"`; do not also read
`event_msg` `user_message` records because they duplicate those turns. Codex
compaction is `event_msg.payload.type: "context_compacted"` in the same file.
Harness-injected role-user setup blocks are retained for completeness and should
be distinguished from the user's actual request while interpreting the result.

If the session-id environment variable is missing or the helper reports an
ambiguous candidate, inspect `analyze-session.sh` output and choose the file
whose session id, working directory, and activity match this agent. Never guess
from a post-compaction summary alone.

## Using the recovered conversation

After running `conversation.sh`:

1. Read all emitted user windows, including the first one and everything after
   every compaction marker.
2. Use the two preceding agent messages to understand what each user turn was
   responding to. Treat an absent preceding message at the beginning as normal.
3. Reconstruct the original problem, pivots, explicit requests, unanswered
   questions, promises, and work that was discussed but not completed.
4. Use that reconstruction as an input to loose-thread and followup analysis.
   Do not silently discard a thread just because the current context no longer
   contains it.

## Other queries

For a compact inventory rather than the full conversation:

```bash
~/.codex/skills/session_logs/analyze-session.sh codex
~/.claude/skills/session_logs/analyze-session.sh claude
```

Pass an explicit transcript path to either helper when reviewing an older
session. The scripts distinguish the two JSONL schemas and count actual user
turns, agent messages, tool calls, and compaction markers without relying on
raw `grep` counts.
