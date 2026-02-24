# AUTOFIXER Hook Specification

Automatically runs pre-commit autofixes on files after Claude Code writes or edits them.

## Configuration

**Event**: `PostToolUse`
**Matchers**: `Edit|MultiEdit|Write`
**Blocking**: Never blocks Claude (always continues processing)
**Feedback**: Provides FYI message to Claude when fixes are applied

## Pre-commit Integration

Autofixer hook relies on a **critical assumption** that all pre-commit hooks are autofix-only and will automatically fix any issues they detect.

File exclusions are handled by pre-commit configuration - the hook does not handle them.

### Execution

1. Detect git repository root
2. Run `pre-commit run --files <file>` (leverages all configured autofix tools)
3. Detect changes via file modification time
4. Provide feedback if autofixes were made

### Error Handling

- Skip if not in git repository or pre-commit not available
- Log errors but never block Claude's workflow
- Timeout after 30 seconds

## Output

- **No changes**: silent success
- **Applied autofixes**: FYI message to Claude: `Autofixes applied: example.py`
- **Error**: Non-blocking message: `⚠️ Autofix failed on example.py: pre-commit error`
