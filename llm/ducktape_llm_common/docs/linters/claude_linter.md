# Claude Linter

## Overview

The Claude linter integrates with Claude Code's hook system to provide real-time linting and automatic fixes during file operations. It leverages pre-commit for validation and auto-fixing, blocking operations that would introduce non-fixable violations.

## Key Features

- **Pre-write validation**: Blocks writes that would introduce non-fixable violations
- **Post-write auto-fixes**: Automatically fixes issues after writes/edits
- **Pre-commit integration**: Uses pre-commit's extensive hook ecosystem
- **Git-aware**: Works both inside and outside Git repositories
- **Clean logging**: Structured JSON logs for debugging

## Installation

The linter is installed as part of ducktape-llm-common:

```bash
pip install -e /path/to/ducktape_llm_common
```

## Usage

### Claude Code Hook Integration

The linter is automatically invoked by Claude Code through its hook system. Configure it in your Claude settings:

```json
{
  "hooks": {
    "PreToolUse": {
      "command": ["claude-linter", "hook"]
    },
    "PostToolUse": {
      "command": ["claude-linter", "hook"]
    }
  }
}
```

The hook command automatically routes to the correct handler based on the `hook_event_name` field in the JSON input.

### Command Line

```bash
# Run hook (automatically routes based on JSON input)
claude-linter hook

# Clean old log files
claude-linter clean

# Clean logs older than N days
claude-linter clean --older-than 7

# Preview what would be cleaned
claude-linter clean --dry-run
```

## Configuration

The Claude linter uses pre-commit for its configuration:

### Configuration Sources

1. **Git Repository Mode**: When working in a Git repository, the linter automatically finds and uses `.pre-commit-config.yaml` by traversing up the directory tree
2. **Non-Git Mode**: When outside a Git repository, it uses configuration provided via Claude settings or a fallback configuration

### Pre-commit Configuration

Create `.pre-commit-config.yaml` in your project:

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.1.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.5.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
```

### Hook Execution

Both pre and post hooks run ALL configured pre-commit hooks with fixing enabled:

- **Pre-hook**: Tests if proposed content can be fully fixed (blocks only if non-fixable issues remain)
- **Post-hook**: Actually applies the fixes to written files

### Disabling

The linter respects pre-commit's standard disabling mechanisms:

- Add `SKIP` environment variable for specific hooks
- Use `# pragma: no cover` or hook-specific ignore comments in code

## Implementation Details

### Hook Behavior

#### Pre-Hook (Write operations only)

1. Receives proposed file content from Claude Code
2. Creates a temporary file with the content
3. Runs pre-commit with fixing enabled on the temp file
4. If fixes were applied, runs again to check if non-fixable issues remain
5. Blocks the write only if non-fixable violations are found
6. Returns JSON response with decision and reason

#### Post-Hook (Write, Edit, MultiEdit operations)

1. Receives information about written/edited files
2. Runs pre-commit with fixing enabled on the actual files
3. Applies auto-fixes directly to the files
4. For Write: Sends "FYI" message to Claude if fixes were applied
5. For Edit/MultiEdit:
   - Always applies all possible auto-fixes
   - If non-fixable violations remain after fixes, warns Claude with detailed message
   - If only auto-fixes were needed, sends "FYI" message to Claude
   - If no changes needed, continues normally
6. Returns JSON response (block decision = warning to Claude, not blocking the operation)

### Logging

- Logs stored in `~/.cache/claude-linter/`
- Named with pattern: `hook-{pre|post}-{timestamp}.json`
- Contains full request/response data for debugging
- Old logs can be cleaned with `claude-linter clean`

### Exit Codes

The linter always exits with code 0 to comply with Claude Code's hook API. Actual decisions are communicated through the JSON response:

```json
{
  "decision": "block",
  "reason": "Non-fixable violations found:\n- Line 10: Missing type annotation",
  "continue": true
}
```

## Future Improvements

- Implement remaining rules (string building, doc checks, etc.)
- Add custom ruff plugin for hasattr detection
- Performance optimization for large codebases
- Integration with pre-commit hooks
- VS Code extension
