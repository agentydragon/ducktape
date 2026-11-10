# difftree

Tree-style visualization of git diffs with progress bars.

## Usage

Run `difftree --help` for the supported diff arguments, columns, sorting options, and examples.

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

Uses syrupy snapshot tests. See <../devinfra/docs/syrupy_snapshots.md> for the workflow.
