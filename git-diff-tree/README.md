# git-diff-tree

Tree-style visualization of git diffs with progress bars.

## Features

- **Tree display**: Shows changed files in a tree structure
- **Progress bars**: Visual representation of additions (green, right-aligned) and deletions (red, left-aligned)
- **Statistics**: Shows +/- counts and percentage of total diff
- **Sorting**: Sort by diff size (default) or alphabetically
- **Configurable**: Hide/show individual column groups
- **Works as a git pager**: Can be used with `git diff` directly

## Installation

```bash
# The development environment uses devenv + direnv
# Once direnv is allowed, the environment activates automatically
direnv allow

# Install package in editable mode
pip install -e .
```

## Usage

### Basic usage

```bash
# Show unstaged changes
git-diff-tree

# Show changes between commits
git-diff-tree HEAD~1 HEAD

# Show staged changes
git-diff-tree --cached

# Show changes in a specific commit
git-diff-tree COMMIT~1 COMMIT
```

### Sorting and display options

```bash
# Sort alphabetically
git-diff-tree --sort alpha

# Hide progress bars
git-diff-tree --no-bars

# Hide counts
git-diff-tree --no-counts

# Hide percentages
git-diff-tree --no-percentages

# Minimal output (only tree structure)
git-diff-tree --no-bars --no-counts --no-percentages

# Adjust progress bar width
git-diff-tree --bar-width 30
```

### As a git pager

You can use git-diff-tree as a custom pager for git diff:

```bash
# One-time use
git diff | git-diff-tree

# Configure globally
git config --global pager.diff "git-diff-tree"

# Configure for specific repository
git config pager.diff "git-diff-tree"
```

## Output format

```
src/
├── git_diff_tree/
│   ├── __init__.py      +2 -0  ████████████        ████░░░░░░░░░░░░░░░░   15.0%
│   ├── parser.py        +65 -0 ████████████████████                       45.2%
│   ├── tree.py          +89 -5 ████████████████████░░░░░░░░░░░░░░░░░░░░   60.5%
│   └── renderer.py      +120 -3████████████████████░░░░░░░░░░░░░░░░░░░░   80.0%
tests/
└── test_parser.py       +45 -0 ████████████████                           30.0%
```

**Columns:**
1. Tree structure with file/directory names
2. Addition count (+X) in green
3. Deletion count (-X) in red
4. Addition progress bar (right-aligned, grows left)
5. Deletion progress bar (left-aligned, grows right)
6. Percentage of total diff

## Development

The project uses devenv for environment management. Once you run `direnv allow`, all dependencies are automatically available.

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=git_diff_tree

# Update snapshot tests
pytest --snapshot-update
```

## Testing

The project includes:
- **Unit tests**: Test individual components
- **E2E tests**: Test with real git operations
- **Snapshot tests**: Committed ANSI renders for regression testing

## License

AGPL-3.0-or-later
