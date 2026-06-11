# TODO - claude_hooks

## Test Coverage Improvements

### Schema Evolution Testing

- [ ] **Test builtin tool with unexpected schema**: Create test that verifies hook behaves reasonably when a builtin tool (Write, Edit, etc.) shows up with unexpected schema
  - Simulate new version of Claude Code adding new tool options/fields
  - Ensure graceful degradation - tool should still parse but not break
  - Test both extra fields (forward compatibility) and missing fields (backward compatibility)

### End-to-End Testing

- [ ] **Subprocess hook invocation tests**: Create end-to-end tests that invoke hook as subprocess and feed it JSON
  - Test actual hook execution pipeline from command line
  - Verify proper error handling and exit codes
  - Test various JSON input scenarios (valid, invalid, malformed)
  - Ensure subprocess communication works correctly

- [ ] **Real Claude integration tests**: Create end-to-end tests involving actual Claude instance
  - Set up isolated test environment
  - Instruct Claude with exact specific tool calls to make
  - Capture and verify hook invocations match expected patterns
  - Test real-world tool usage scenarios
  - Verify hook responses don't interfere with Claude's operation

## Test Data Collection

- [ ] **Expand JSON test coverage**: Current coverage is 13/89 scenarios (~15%)
  - **Priority:** Edit tool variations
  - **Priority:** MultiEdit tool variations
  - Grep tool variations (most gaps)
  - NotebookRead/NotebookEdit tools
  - Individual TodoWrite status/priority scenarios

## Architecture & Robustness

- [ ] **Hook error recovery**: Ensure hooks handle unexpected failures gracefully
- [ ] **Pre-commit unexpected failures**: If pre-commit exits in an unexpected way, show feedback message in Claude Code UI (not just log)

## UserPromptSubmit Hook Research

- [ ] **UserPromptSubmit JSON protocol**: According to docs/reference, it's possible to use UserPromptSubmit to inject stuff into context, but their demo only shows how to do it with exit code signalling, not with JSON. The documentation does not explain how to do it with JSON. It would be nice to know how to do it through JSON. Worst case, we could special-case it as a case where we allow non-JSON output & exit code signalling instead of requiring JSON protocol compliance.
