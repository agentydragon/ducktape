@README.md

## Agent Development Patterns

### Adding a New Linter

1. Create a new module in `ducktape_llm_common/claude_linter/linters/`
2. Implement the linter function with proper type annotations
3. Register it in the appropriate mode in `registry.py`
4. Add tests parallel to production code in `ducktape_llm_common/claude_linter/`

### Testing Patterns

- Tests live parallel to production code (e.g., `ducktape_llm_common/claude_linter/test_*.py`)
- Use pytest fixtures for common test data
- Mock external dependencies (file system, git operations)
- Test both success and failure cases

## Important Notes

- This library is the canonical place for globally useful LLM tools - avoid creating similar utilities in individual projects
- The claude linter is designed to integrate with Claude Code's hook system
- All code should maintain Python 3.13+ compatibility
- Configuration files use TOML format with Pydantic validation
