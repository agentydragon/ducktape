Handle and systematically prevent bad patterns observed in the current work.

When invoked, follow this process to turn a single bad example into systematic improvement:

## Phase 1: Clarify the Issue

1. **Identify what's bad**:
   - If not clear from context, ask: "What specifically is the bad pattern here?"
   - Get concrete example of the bad code/approach
   - Understand WHY it's bad (performance, maintainability, security, style, etc.)

2. **Determine scope**:
   - Ask if unclear: "Is this a global preference (all your projects) or local to this project?"
   - **Local**: Apply to current project only
   - **Global**: Apply to both current project AND ~/.claude/CLAUDE.md

## Phase 2: Create Action Plan (using TodoWrite)

### Todo 1: Document the antipattern
- Update appropriate files:
  - **Local**: Project's CLAUDE.md, README.md, or CONTRIBUTING.md
  - **Global**: Also update ~/.claude/CLAUDE.md
- Include:
  - Clear description of the bad pattern
  - Specific example from current occurrence
  - Why it's problematic
  - Good alternative with example
  - Format for easy LLM understanding

### Todo 2: Automate detection (if possible)
- Identify if this can be caught by:
  - ESLint rule (JavaScript/TypeScript)
  - Ruff/flake8/pylint rule (Python)
  - Pre-commit hook
  - Custom linter/grep pattern
  - Type system constraints
- If yes:
  - Configure the tool
  - Add to pre-commit config
  - Update project docs to mention running these checks
  - Test it catches the bad pattern

**Special cases with known solutions:**
- **hasattr/getattr/setattr in Python**: Add pre-commit hook with:
  ```yaml
  - repo: local
    hooks:
      - id: no-hasattr-getattr
        name: Check for hasattr/getattr/setattr usage
        entry: '(hasattr|getattr|setattr)\s*\('
        language: pygrep
        types: [python]
        exclude: '^(tests/|test_)'  # May need in tests
  ```
- **String concatenation for URLs/SQL/HTML**: Use pygrep/grep patterns
- **Empty except blocks**: Most Python linters catch this
- **Redundant docstrings**: Can use custom AST-based checker

### Todo 3: Fix existing instances
- **Spawn parallel agents** to:
  - Search for other instances of this pattern
  - Group by file/module
  - Fix where straightforward
  - Flag complex cases for manual review

## Phase 3: Execute Plan

Execute todos, spawning parallel agents where beneficial:
- **Agent A**: Search codebase for pattern instances
- **Agent B**: Research appropriate linting rules
- **Agent C**: Draft documentation updates
- **Agent D**: Fix simple instances

## Phase 4: Verify & Commit

1. Run any new linters/checks to verify they work
2. Ensure documentation is clear
3. Commit changes with message explaining the improvement

## Example Usage

### Example 1: Direct feedback
User: /bad this is bad error handling you gotta actually log this
Assistant: I understand - the error handling here swallows the exception without logging. Is this a global preference (always log errors before handling) or specific to this project?

User: global

### Example 2: Context-based identification
User: /bad
Assistant: I see you used string concatenation to build a URL on line 47. Is this the bad pattern?

User: yes, never use string concat for structured data

### Example 3: Scope specified upfront
User: /bad global: way too verbose docstring just repeats what arguments already say
Assistant: Understood - avoiding redundant docstrings is a global preference. I'll document this and search for other verbose docstrings.

### Example 4: Unclear feedback
User: /bad this is terrible
Assistant: I need more specifics. What makes this code terrible? Is it:
- The error handling approach?
- The variable naming?
- The algorithm complexity?
- The code structure?

User: the way you're building the config dict with all those if statements
