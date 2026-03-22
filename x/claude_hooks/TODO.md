# TODO - claude_hooks

## Test Coverage

- [ ] Schema evolution tests: verify hooks handle unexpected/missing fields gracefully (forward/backward compatibility)
- [ ] Subprocess E2E tests: invoke hook as subprocess with JSON stdin, verify output and exit codes
- [ ] Real Claude integration tests: instruct Claude to make specific tool calls, verify hook invocations
- [ ] Expand JSON test fixtures: current 13/89 scenarios (~15%). Priority: Edit, MultiEdit, Grep, NotebookRead/Edit

## Architecture

- [ ] Hook error recovery: surface pre-commit unexpected failures in Claude Code UI, not just logs

## Research

- [ ] UserPromptSubmit JSON protocol: docs only show exit code signalling, not JSON. Investigate JSON-based context injection.
