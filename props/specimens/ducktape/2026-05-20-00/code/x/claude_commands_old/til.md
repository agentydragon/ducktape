Document learnings for future AI agents working on this project.

When invoked, capture important discoveries, patterns, or insights that would help future AI agents work more effectively with this codebase.

## Actions

1. **Identify the learning**:
   - What was discovered/learned?
   - Why is it non-obvious or important?
   - What context makes this valuable?

2. **Choose location**:
   - Default: `./agents/til/` directory (create if needed)
   - Fallback: `./agents/TIL.md` if directory creation fails
   - For global learnings: `~/.claude/agents/til/`
   - Rationale: `agents/` clearly indicates AI-agent-specific knowledge

3. **Document the learning**:
   - Create file with descriptive name: `YYYY-MM-DD-brief-description.md`
   - Or append to `TIL.md` with date header
   - Format for AI consumption (clear, contextual)

## Documentation Format

```markdown
# [Brief Title]

**Date**: YYYY-MM-DD
**Agent**: [Your agent name - run generate-agent-name at start of session]
**Context**: [What you were trying to do]

## Key Learning

[Concise statement of what was learned]

## Details

[Explanation that would help another AI agent]

## Examples

[Code examples if applicable]

## Gotchas

[What to watch out for]

---

_Documented by [agent-name]_
```

## Example Usage

### Example 1: API behavior discovery

U: /til
A: What did we learn that future agents should know?

U: the Firebase API silently drops messages over 16KB without error

[Creates file: ./agents/til/2025-01-18-firebase-message-size-limit.md documenting this behavior]

### Example 2: Code pattern realization

U: turns out all the translog operations need unique timestamps or they merge /til
A: I'll document this critical timing requirement for future agents.

[Adds to ./agents/til/2025-01-18-translog-unique-timestamps.md with examples]

### Example 3: Non-obvious workaround

U: /til you have to use the placeholder dance pattern or nodes show "Updated Placeholder"
A: This is crucial for node creation. I'll document the pattern clearly.

[Creates detailed explanation with code examples showing the right transaction boundaries]
