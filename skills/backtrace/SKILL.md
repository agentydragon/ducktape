---
name: backtrace
description: Show the current task stack and context, reconstructing it from the complete session transcript across compaction boundaries. Use when user says "bt", "backtrace", "stack", "where are we", or asks about current progress on a multi-step task.
---

Show the current task stack and context, but recover durable conversation history first.

## Mandatory history recovery

Before producing a backtrace, use the `session_logs` skill. Do not assume that the
visible context, a post-compaction summary, or the last few turns contains the full
task history.

1. Identify the current harness (Claude Code or Codex CLI) and use
   `session_logs` to find the current parent transcript. If the session-id helper
   finds multiple candidates, follow `session_logs`'s disambiguation procedure;
   never guess from recency alone when the candidates are ambiguous.
2. Run the `session_logs` analysis and conversation recovery commands. Read the
   complete conversation output, including every user window before and after every
   compaction marker. For large transcripts, read sequential chunks and verify the
   final actual user-message number; do not use `head`, `tail`, or a truncating pager
   as the recovery pass.
3. Reconstruct the full task tree from that history: original objectives, pivots,
   explicit requests, completed work, promises, unresolved threads, blockers, and
   decisions. Preserve threads that disappeared from the current context after
   compaction.
4. Cross-check the recovered history against the current visible context and live
   workspace state. Distinguish actual user requests from harness setup, system
   reminders, assistant suggestions, and stale historical status.

If transcript recovery fails or remains ambiguous, report a context-limited partial
backtrace and make the visibility problem a blocker. Never present the current
compacted summary plus visible context as a complete backtrace. If recovery succeeds,
briefly report the transcript/harness and compaction count in the result; zero
compaction markers still means the complete transcript was checked.

Provide a clear summary of:

1. Current task depth and what we're doing at each level
2. Where we are in the current task
3. What remains to be done
4. Any blockers or pending decisions

## Format

Use a visual stack representation showing the task hierarchy:

```
[Task 1: Main objective]
└─[Task 2: Subtask we pivoted to]
  └─[Task 3: Current focus] ← YOU ARE HERE
    - ✓ Completed step
    - ⏳ Current step
    - ○ Remaining step
```

## Key Points

- Be concise but complete
- Show the full context stack
- Indicate current position clearly
- List what's completed, in progress, and remaining
- Mention any blockers or decisions needed
- Include counts/quantities where helpful
