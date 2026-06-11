# Split Feature Design

## Overview

A new feature for `wt` that allows splitting large PRs by moving files from the current branch to a new worktree. This addresses the common scenario where a PR grows too large and needs to be split into smaller, more reviewable chunks.

## Motivation

- Large PRs are harder to review and understand
- Sometimes you realize mid-development that changes should be split
- Need a clean way to extract files/changes while maintaining git history
- Should feel natural and integrated with existing worktree workflow

## Core Functionality

### High-Level Flow

1. Identify files/changes to move from current branch
2. Create a new worktree for the extracted changes
3. Remove the changes from current branch
4. Leave current branch in clean state
5. New worktree contains the extracted changes ready for independent PR

### Command Interface

#### Basic Mode: File List

```bash
wt split <new-worktree-name> <file1> <file2> ...
wt split my-new-feature src/feature.py src/feature_test.py
```

#### Interactive Mode

```bash
wt split --interactive <new-worktree-name>
wt split -i my-new-feature
```

## Interactive Mode Design

### REPL Interface

Similar to `git add --interactive` but for file/change movement:

```
Split Plan for 'my-new-feature':
Files to move:
  1. src/feature.py (modified, +150 -20)
  2. src/feature_test.py (new file, +80)
  3. docs/feature.md (modified, +30 -5)

Commands:
  [s]how plan
  [a]dd file
  [r]emove file
  [p]atch (edit chunks)
  [e]xecute plan
  [q]uit

What now>
```

### Interactive Commands

#### Show Plan (`s`)

Display current plan with file status and change summary

#### Add File (`a`)

```
Add file> src/another_file.py
Added src/another_file.py to split plan
```

#### Remove File (`r`)

```
Remove file> 2
Removed src/feature_test.py from split plan
```

#### Patch Mode (`p`)

Enter patch-style editing for specific files:

```
Patch file> 1
# Shows hunks for src/feature.py
Hunk 1/3: @@ -10,6 +10,15 @@
 def existing_function():
     pass

+def new_feature_function():
+    return "new feature"
+
 def another_function():
     pass

[y]es move this hunk, [n]o keep in current branch, [s]kip to next file:
```

#### Execute (`e`)

Execute the split plan:

- Create new worktree
- Move selected files/hunks
- Clean up current branch
- Show summary of what was moved

#### Quit (`q`)

Exit without making changes

## Implementation Considerations

### Git Operations

#### File Movement Strategy

1. **Full File Move**: Simple case - entire file moves to new worktree
2. **Partial File Move**: Complex case - only some hunks move, file exists in both branches

#### For Full File Moves

```bash
# In current branch
git rm <file>
git commit -m "Move <file> to separate branch"

# In new worktree
git checkout current-branch -- <file>
git add <file>
git commit -m "Add <file> from main branch"
```

#### For Partial File Moves

1. Use `git apply` with patch splitting
2. Create patches for hunks to move vs hunks to keep
3. Apply appropriate patches to each branch

### Branch Management

#### New Worktree Creation

```bash
# Create new branch from current branch
git checkout -b <new-branch-name>
# Create worktree for new branch
git worktree add <worktree-path> <new-branch-name>
```

#### Branch Naming

- Default: `split-<timestamp>-<description>`
- User-provided: use as-is
- Conflict resolution: append suffix

### Safety Considerations

#### Pre-flight Checks

- Ensure working directory is clean (or handle dirty state)
- Verify no conflicts with existing worktrees/branches
- Check that files to move actually exist and are tracked

#### Rollback Strategy

- Create backup refs before starting
- Support `wt split --abort` to cancel in-progress split
- Store split state in `.git/wt-split-state` during operation

### Integration with Existing Features

#### With `wt create`

- Should feel natural extension of existing workflow
- Reuse existing branch/worktree creation logic
- Consistent naming patterns and directory structure

#### With `wt list`/`wt status`

- Show split worktrees with context about origin
- Indicate which worktrees are splits vs fresh creates

## User Experience Examples

### Example 1: Simple File Split

```bash
$ git status
On branch feature/big-change
Changes to be committed:
  modified:   src/feature_a.py
  new file:   src/feature_b.py
  modified:   tests/test_both.py

$ wt split feature-b-only src/feature_b.py
Creating worktree 'feature-b-only'...
Moving src/feature_b.py to new worktree...
Cleaned up current branch.

Current branch now has:
  modified:   src/feature_a.py
  modified:   tests/test_both.py

New worktree 'feature-b-only' has:
  new file:   src/feature_b.py
```

### Example 2: Interactive Split

```bash
$ wt split -i auth-refactor

Split Plan for 'auth-refactor':
  1. src/auth.py (modified, +200 -50)
  2. src/middleware.py (modified, +30 -10)
  3. tests/test_auth.py (modified, +100 -20)

What now> p
Patch file> 1

Hunk 1/4: Authentication class refactor
[y]es move, [n]o keep, [s]kip file: y

Hunk 2/4: New login method
[y]es move, [n]o keep, [s]kip file: y

Hunk 3/4: Backwards compatibility fix
[y]es move, [n]o keep, [s]kip file: n

Hunk 4/4: Debug logging
[y]es move, [n]o keep, [s]kip file: n

What now> e
Executing split plan...
✓ Created worktree 'auth-refactor'
✓ Moved 2/4 hunks from src/auth.py
✓ Moved complete files: tests/test_auth.py
✓ Cleaned up current branch
```

## Command Line Interface

### New Commands

#### `wt split`

```
wt split [options] <worktree-name> [files...]

Options:
  -i, --interactive    Interactive mode for selecting files/hunks
  -b, --base-branch    Base branch for new worktree (default: current)
  -m, --message        Commit message template
  --dry-run           Show what would be done without executing
  --abort             Cancel in-progress split operation

Arguments:
  worktree-name       Name for new worktree and branch
  files               Files to move (ignored in interactive mode)
```

#### `wt split-status`

```bash
wt split-status    # Show any in-progress split operations
```

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
