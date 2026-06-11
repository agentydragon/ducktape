# Claude Code Linter Hooks

This document describes the Claude Code hooks that automatically run `claude-linter` on files edited by Claude.

## Overview

The hooks integrate with Claude Code's hooks system to:

- **Pre-hook**: Run the linter before Write operations to block violations
- **Post-hook**: Auto-fix text issues and Python formatting after Write/Edit/MultiEdit operations

Both hooks ensure code quality standards are maintained.

## Setup

1. **Hook Scripts**: The hook logic is in:
   - Pre-hook: `ducktape_llm_common/claude_linter/claude_pre_hook.py`
   - Post-hook: `ducktape_llm_common/claude_linter/claude_post_hook.py`
   - Unified CLI: `ducktape_llm_common/claude_linter/claude_linter.py`

2. **Installation**: The hooks are automatically installed when you install the package:

   ```bash
   pip install -e /path/to/ducktape_llm_common
   # or
   pip install ducktape-llm-common
   ```

   This installs the `claude-linter` command with pre/post/check modes.

3. **Configuration**: The hooks are configured in `~/.claude/settings.json`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Write",
           "hooks": [
             {
               "type": "command",
               "command": "claude-linter pre",
               "continue": true
             }
           ]
         }
       ],
       "PostToolUse": [
         {
           "matcher": "Write",
           "hooks": [
             {
               "type": "command",
               "command": "claude-linter post",
               "continue": true
             }
           ]
         },
         {
           "matcher": "Edit",
           "hooks": [
             {
               "type": "command",
               "command": "claude-linter post",
               "continue": true
             }
           ]
         },
         {
           "matcher": "MultiEdit",
           "hooks": [
             {
               "type": "command",
               "command": "claude-linter post",
               "continue": true
             }
           ]
         }
       ]
     }
   }
   ```

## How It Works

### Pre-Hook (Write only)

1. **Triggering**: Runs before Claude creates new files with Write tool
2. **Python File Detection**: Checks if the file will be a Python file (`.py` extension)
3. **Text Fixes**: Runs pre-commit text fixes on all file types
4. **Python Linting**:
   - Creates a `ClaudeRulesLinter` instance with `treat_all_as_errors=True`
   - This mode treats ALL violations as errors (not just "new" ones)
   - Runs the linter to check for violations
5. **Results**:
   - **Success**: Exits with code 0, allows file creation
   - **Failure**: Exits with code 2, blocks creation and shows errors to Claude

### Post-Hook (Write/Edit/MultiEdit)

1. **Triggering**: Runs after Claude modifies files
2. **Text Fixes**: Automatically fixes:
   - Trailing whitespace (preserves markdown double-space line breaks)
   - Missing final newlines
   - Mixed line endings (converts to LF)
   - UTF-8 BOM
3. **Python Fixes**: For `.py` files, also runs:
   - `ruff format` for code formatting
   - `ruff check --fix` for auto-fixable violations
4. **Results**: Always exits with code 0, reports what was fixed

## Behavior

- Pre-hook blocks violations that can't be auto-fixed
- Post-hook auto-fixes what it can
- Text fixes apply to all supported file types (.py, .js, .md, .txt, .rs, etc.)
- Python-specific fixes only apply to .py files

## Testing

To test the hooks manually:

```bash
# Test pre-hook (expects JSON on stdin)
echo '{"tool_name": "Write", "tool_input": {"file_path": "/path/to/test.py"}}' | claude-linter pre

# Test post-hook (expects JSON on stdin)
echo '{"tool_name": "Write", "tool_input": {"file_path": "/path/to/test.py"}}' | claude-linter post

# Manual check mode (no stdin required)
claude-linter check                    # Check all Python files in current directory
claude-linter check file.py           # Check specific file
claude-linter check src/              # Check all Python files in directory
```

## Troubleshooting

- Check that `claude-linter` is in PATH: `which claude-linter`
- View debug logs: `/tmp/claude-linter-hook.log` and `/tmp/claude-post-hook.log`
- Verify the hooks are configured in `~/.claude/settings.json`
- Ensure the package is installed: `pip show ducktape-llm-common`

## CLI Usage

The `claude-linter` command supports three modes:

1. **`claude-linter pre`** - Pre-hook mode (blocks violations)
   - Used by Claude Code before Write operations
   - Reads JSON from stdin
   - Exit code 2 blocks the operation

2. **`claude-linter post`** - Post-hook mode (auto-fixes)
   - Used by Claude Code after Write/Edit/MultiEdit operations
   - Reads JSON from stdin
   - Always exits with code 0

3. **`claude-linter check [files...]`** - Manual check mode
   - For users to manually check files
   - No stdin required
   - Exit code 1 if violations found

## Related Files

- Main linter CLI: `ducktape_llm_common/claude_linter/claude_linter.py`
- Pre-hook logic: `ducktape_llm_common/claude_linter/claude_pre_hook.py`
- Post-hook logic: `ducktape_llm_common/claude_linter/claude_post_hook.py`
- Linter config: `.claude-linter.json` (project-specific)
