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
   - **Global**: Apply to ~/.claude/CLAUDE.md and/or create learning file

## Phase 2: Create Action Plan (using TodoWrite)

### Todo 1: Document the antipattern

- Update appropriate files:
  - **Local**: Project's CLAUDE.md, README.md, or CONTRIBUTING.md
  - **Global**:
    - **CLAUDE.md** (RARE): Only for truly universal principles that apply to EVERY session
      - Examples: "Never swallow exceptions", "No hasattr/getattr", "Evidence required for claims"
      - High bar: Must be fundamental enough to deserve precious token space
    - **Learning file** (COMMON): ~/.claude/learnings/YYYY-MM-DD-pattern-name.md
      - For specific problems, tools, patterns
      - Examples: "Use comby for refactoring", "Never parse HTML with regex"
      - Searchable when stuck, doesn't bloat core instructions
- Include:
  - Clear description of the bad pattern
  - Specific example from current occurrence
  - Why it's problematic
  - Good alternative with example

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
        exclude: "^(tests/|test_)" # May need in tests
  ```

- **String concatenation for URLs/SQL/HTML**: Use pygrep/grep patterns
- **Empty except blocks**: Most Python linters catch this
- **Redundant docstrings**: Can use custom AST-based checker

### Todo 3: Fix existing instances

- Search for other instances of this pattern
- Use appropriate tools:
  - `rg` for simple text patterns
  - `ast-grep` for code structure patterns
  - `comby` for systematic refactoring
- Fix where straightforward
- Flag complex cases for review

### Todo 4: Create learning if pattern-specific

- If the bad pattern is situational (not universal):
  - Create ~/.claude/learnings/YYYY-MM-DD-bad-pattern.md
  - Include searchable symptoms
  - Document the solution
  - Run reindex-learnings.sh

## Phase 3: Execute Plan

Execute todos systematically:

1. Document first (prevents forgetting the lesson)
2. Set up automation (catches future instances)
3. Fix existing code (cleans up current state)
4. Create learning file if needed (helps when stuck later)

## Phase 4: Verify & Commit

1. Run any new linters/checks to verify they work
2. Ensure documentation is clear
3. Test that bad pattern is caught by automation
4. Commit changes with message explaining the improvement

## Example Usage

### Example 1: Direct feedback

U: /bad this is bad error handling you gotta actually log this
A: I understand - the error handling here swallows the exception without logging. Is this a global preference (always log errors before handling) or specific to this project?

U: global

### Example 2: Context-based identification

U: /bad
A: I used string concatenation to build a URL on line 47. Is this the bad pattern?

U: yes, never use string concat for structured data

### Example 3: Scope specified upfront

U: /bad global: way too verbose docstring just repeats what arguments already say
A: Understood - avoiding redundant docstrings is a global preference. I'll document this and search for other verbose docstrings.

### Example 4: Pattern worth a learning file

U: /bad you're parsing HTML with regex again
A: You're right - I used regex for HTML parsing which breaks on nested tags. This is a common mistake worth documenting. I'll:

1. Create a learning file for "never parse HTML with regex" (not fundamental enough for CLAUDE.md)
2. Set up a pre-commit check to catch this
3. Search for other regex HTML parsing in the codebase

## Key Principles

- **One bad example → Systematic prevention**
- **CLAUDE.md = Fundamental principles only** (high bar for token space)
- **Learnings = Specific patterns/tools/solutions** (searchable when stuck)
- **Local patterns → Project documentation**
- **Automate detection when possible**
- **Fix existing instances to prevent drift**
