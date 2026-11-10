# Split Feature Design

## Status and scope

Proposal only: `wt split` and `wt split-status` are not implemented. The feature would
let a large change be split during development into smaller reviewable changes by
extracting selected files or hunks into a new worktree. Preserving detailed commit
history is an open question below.

## Proposed interface

```text
wt split [options] <worktree-name> [files...]
wt split -i <worktree-name>
wt split-status
```

| Option                | Proposed behavior                                                 |
| --------------------- | ----------------------------------------------------------------- |
| `-i`, `--interactive` | Select files and hunks in a REPL; positional files are ignored.   |
| `-b`, `--base-branch` | Base branch for the new worktree; defaults to the current branch. |
| `-m`, `--message`     | Commit-message template.                                          |
| `--dry-run`           | Show the planned operation without executing it.                  |
| `--abort`             | Cancel an in-progress split.                                      |

`<worktree-name>` names the new worktree and branch. Without interactive mode,
`[files...]` selects whole files.

## Selection and execution

| Selection         | Proposed behavior                                                                                                                                          |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Whole files       | Move each selected file to the new worktree and remove it from the source branch.                                                                          |
| Hunks             | A file may remain in both branches: selected hunks move; unselected hunks stay in the source.                                                              |
| Patch application | Split selected and retained hunks into separate patches and apply each with `git apply`; this is proposed, not a runnable procedure.                       |
| Interactive plan  | Show file status and change summary; `s` shows the plan, `a` adds a file, `r` removes one, `p` selects hunks, `e` executes, and `q` exits without changes. |
| Hunk prompt       | `y` moves this hunk, `n` keeps it in the source, and `s` skips to the next file.                                                                           |
| Completion        | Create the destination worktree, move selected content, remove those changes from the source, and summarize the result.                                    |

The earlier overview says the source branch ends clean, while both examples retain
unselected source changes. Resolve that source-state contract before implementation.

## Branch, safety, and integration

- Use the current branch as the default base; `-b, --base-branch` can select
  another. The worktree name also names the new branch: default to
  `split-<timestamp>-<description>`, use a supplied name as-is, and append a
  suffix on collision.
- Before executing, check whether the source worktree is clean or define how dirty
  state is handled; verify selected paths exist and are tracked, and that the new
  branch/worktree does not conflict with an existing one.
- Create backup refs before the operation. Persist in-progress state in
  `.git/wt-split-state`; `--abort` cancels an operation and `wt split-status`
  reports its state.
- Full-file operations and rollback need a safe design; exact commit-history
  preservation remains open.
- Reuse `wt create`'s branch/worktree logic and naming/layout conventions. `wt
list` and `wt status` should identify a split worktree and its origin.

## Future Enhancements

### GitHub Integration

- Automatically create draft PR for new worktree
- Link PRs with comments about the split
- Update original PR description to mention split

### Smart Suggestions

- Analyze file dependencies to suggest related files
- Warn about potential broken references
- Suggest test files that should move with implementation

### Undo Support

- `wt split --undo <worktree-name>` to merge changes back
- Track split history for easier merging

## Open Questions

1. **Merge Conflicts**: How to handle when files have conflicts between hunks?
2. **Dependencies**: Should we analyze import/dependency relationships?
3. **Test Coverage**: How to ensure moved code doesn't break existing tests?
4. **Commit History**: Should we preserve detailed commit history during splits?
5. **Integration**: Should this be a separate command or integrate with existing `wt create`?

## Implementation Priority

### Phase 1: Basic File Movement

- Simple file list mode
- Full file moves only
- Basic safety checks

### Phase 2: Interactive Mode

- REPL interface
- File selection/deselection
- Execute/abort functionality

### Phase 3: Patch-Level Splitting

- Hunk-level selection
- Partial file moves
- Advanced git operations

### Phase 4: Enhanced UX

- Smart suggestions
- GitHub integration
- Undo/merge-back support
