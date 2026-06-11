# difftree

Tree-style visualization of git diffs with progress bars.

## Features

- **Tree display**: Shows changed files in a tree structure
- **Progress bars**: Visual representation of additions (green, right-aligned) and deletions (red, left-aligned)
- **Statistics**: Shows +/- counts and percentage of total diff
- **Sorting**: Sort by diff size (default) or alphabetically
- **Configurable**: Hide/show individual column groups
- **Works as a git pager**: Can be used with `git diff` directly

## Usage

```bash
difftree # Show unstaged changes
difftree HEAD~1 HEAD
difftree --cached
difftree COMMIT~1 COMMIT
difftree --sort alpha # Sort alphabetically

# Customize columns (tree, counts, bars, percentages)
difftree --columns tree,counts

# Adjust progress bar width
difftree --bar-width 30

# Combine options
difftree --sort alpha --columns tree,counts --bar-width 30
```

### As a git pager

You can use difftree as a custom pager for git diff:

```bash
# One-time use
git diff | difftree

# Configure globally
git config --global pager.diff "difftree"

# Configure for specific repository
git config pager.diff "difftree"
```

## Development

```bash
pytest
pytest --cov=git_diff_tree
pytest --snapshot-update  # Update snapshot tests
```
