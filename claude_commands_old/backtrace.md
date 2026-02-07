Show the current task stack and context.

Aliases: bt, stack

When invoked, provide a clear summary of:

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

## Example Usage

U: /backtrace
A:

```
[Git commit organization for 48 files]
└─[Creating individual commits]
  └─[Handling /bad command feedback] ← YOU ARE HERE
    - ✓ Read command files to verify contents
    - ✓ Updated ~/.claude/CLAUDE.md with antipattern
    - ⏳ Fix commit descriptions
    - ○ Continue with remaining commits
```

Still need to commit:

- 2 more Claude commands (raihelp, memorize)
- llm/html refactoring (6 files)
- ansible configs (3 files)
- misc files (4 items)

Waiting on: User to select which commits to apply

## Key Points

- Be concise but complete
- Show the full context stack
- Indicate current position clearly
- List what's completed, in progress, and remaining
- Mention any blockers or decisions needed
- Include counts/quantities where helpful
