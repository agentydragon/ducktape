---
name: backtrace
description: Show the current task stack and context. Use when user says "bt", "backtrace", "stack", "where are we", or asks about current progress on a multi-step task.
---

Show the current task stack and context.

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
