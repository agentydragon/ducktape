# Testing Claude Code Hooks

Simple testing approach for Claude Code hooks with unit tests and integration testing using real Claude sessions.

## Testing Approach

1. **Unit Tests**: Individual hook components in isolation (pytest)
2. **Integration Tests**: End-to-end testing with real Claude Code
3. **Record+Replay**: Capture hook I/O for regression testing

## Unit Tests

Run the existing unit tests:

```bash
pytest tests/ -v
```

Current test coverage:

- `tests/test_autofixer.py` - Pre-commit autofixer hook logic
- `tests/test_actions.py` - Hook action helpers

## Integration Testing with Real Claude

### Simple End-to-End Test

```bash
# Set up test directory
mkdir /tmp/claude_hook_test && cd /tmp/claude_hook_test
git init
git config user.name "Test" && git config user.email "test@test.com"

# Create pre-commit config
cat > .pre-commit-config.yaml << 'EOF'
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.1.0
    hooks:
      - id: ruff
        args: [--fix, --exit-non-zero-on-fix]
      - id: ruff-format
EOF

# Configure Claude with hooks (create .claude/settings.json as needed)

# Test: Ask Claude to write terrible code
claude --debug --json -p "Write a Python file with terrible formatting, bad imports, missing docstrings, and poor practices" > test_output.json

# Verify results
ruff check *.py  # Should pass (hooks fixed the code)
jq '.tool_calls[] | select(.tool_name == "Write")' test_output.json  # Check hook fired

# Compare: Without hooks, the code should fail linting
# (Test this by temporarily disabling hooks)
```

## Maybe Future: Record+Replay for Regression Testing

- Record all hook executions during real Claude sessions for later replay
- Replay recorded sessions to detect regressions:
