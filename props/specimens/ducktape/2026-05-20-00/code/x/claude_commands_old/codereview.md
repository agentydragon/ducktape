# Comprehensive Code Review

Perform a thorough code review of the specified target, which can be:

- An entire codebase
- A directory or module (e.g., `pattern_extractor/*.py`)
- A single file
- A specific function (e.g., `extract_detected_patterns`)

The review scales automatically - analyzing architecture for large codebases, module design for directories, or implementation details for individual functions. Follow all patterns and guidelines defined in CLAUDE.md and any project-specific instructions.

**Scaling the Review**:

- **Full codebase**: Include all phases (architecture, dependencies, maintainability, etc.)
- **Module/directory**: Focus on module design, internal consistency, and integration points
- **Single file**: Skip architecture, focus on implementation quality and patterns
- **Single function**: Deep dive on algorithm, edge cases, and implementation details

Only include review sections relevant to both the scope and project type.

## Review Structure

### Phase 1: High-Level Architecture Analysis (Sequential)

1. **Architecture & Design Review**
   - Overall system architecture and component relationships
   - Design patterns usage and consistency
   - Separation of concerns and modularity
   - API design and boundaries between components
   - Data flow and state management patterns

2. **Dependencies & Coupling Analysis**
   - External dependency audit (necessity, alternatives, security)
   - Inter-module coupling and cohesion
   - Circular dependency detection
   - Abstraction levels and interface design
   - Dependency injection patterns

3. **Maintainability & Evolution**
   - Code organization and project structure
   - Naming conventions and consistency
   - Documentation completeness and accuracy
   - Test coverage and testing strategy
   - Configuration management
   - Error handling patterns
   - Logging and observability

4. **Documentation Audit**
   - Accuracy: Documentation matches current implementation (no lies or outdated info)
   - References: All mentioned files, APIs, and features actually exist
   - Currency: Examples and code snippets work with current version
   - Interlinking: Proper cross-references between related docs
   - Duplication: No redundant documentation across multiple files
   - Dead links: All internal and external links are valid
   - Coverage: Important features and APIs are documented
   - Consistency: Terminology and style are uniform across docs

### Phase 2: Module-Level Analysis (Parallelized with Task agents)

For each major module/component, spawn parallel Task agents to analyze:

1. **Code Quality Patterns**
   - Early bailout patterns (guard clauses)
   - DRY violations and duplication
   - Function/class size and complexity
   - Single Responsibility Principle adherence
   - Type safety and validation patterns

2. **Implementation Review**
   - Algorithm efficiency and optimization opportunities
   - Resource management (memory, file handles, connections)
   - Concurrent programming patterns and race conditions
   - Security vulnerabilities and input validation
   - Performance bottlenecks

3. **Readability & Style**
   - Code clarity and self-documentation
   - Comment quality (avoiding redundant documentation)
   - Variable and function naming
   - Consistent formatting and style
   - Cognitive complexity

### Phase 3: Pattern Compliance

Check for compliance with all applicable coding patterns, conventions, and guidelines defined in:

- CLAUDE.md (global instructions)
- Project-specific CLAUDE.md overrides
- Any documented team conventions
- Language-specific best practices
- Framework-specific patterns

Ensure the code follows all established patterns for error handling, type safety, documentation standards, naming conventions, and architectural principles as defined in the project's governing documents.

### Phase 4: Implementation Planning

Based on the findings, create an actionable plan for addressing the most important issues. You don't need to plan out every single issue - focus on planning fixes for at least 50% of the significant issues, prioritizing those with the highest impact:

1. **Prioritization Strategy**
   - Group related issues that should be fixed together
   - Identify dependencies between fixes
   - Consider risk vs. benefit for each change
   - Account for available resources and timeline

2. **Implementation Roadmap**
   - **Immediate fixes** (critical bugs, security issues): Address within days
   - **Short-term improvements** (high-priority issues): 1-2 week sprint
   - **Medium-term refactoring** (architectural improvements): 1-3 month timeline
   - **Long-term evolution** (nice-to-have improvements): Backlog items

3. **Execution Approach**
   - Which issues can be fixed incrementally vs. requiring larger refactors
   - What can be automated (linting rules, formatting, simple refactors)
   - What requires careful manual work
   - Suggested order of operations to minimize risk

4. **Success Criteria**
   - How to verify each fix is complete and correct
   - What tests need to be added or updated
   - How to prevent regression

## Output Format

**Note**: The following template is a guideline to ensure important aspects aren't forgotten. Prioritize clear communication and usefulness to the reader over rigid adherence to the format. Adapt the structure as needed to best serve the goal of improving the codebase. Skip sections that add no value, expand sections that need more detail, and reorganize as makes sense for the specific project.

### Findings Report Structure

```markdown
# Code Review Findings

## Executive Summary

Overall summary of codebase health, recommended important issues to address if any,
recommendations for addressing them.

## Critical Issues (Must Fix)

1. **[Component] Issue Title**
   - Description: What's wrong
   - Impact: Why it matters
   - Location: `file:line`
   - Recommendation: Specific fix
   - Example: Before/after code if applicable

## High Priority Improvements

[Similar structure]

## Medium Priority Suggestions

[Similar structure]

## Low Priority / Style Issues

[Similar structure]

## Architectural Concerns & Open Questions

Issues that are clearly problematic but lack obvious solutions:

1. **[Component] Ambiguous Design Issue**
   - What's weird: Description of the smell/concern
   - Why it's unclear: What makes this hard to fix
   - Potential approaches:
     a) Option 1: Description, pros/cons
     b) Option 2: Description, pros/cons
     c) Option 3: Description, pros/cons
   - Needs input on: What decisions/clarifications are needed

## Legacy & Technical Debt Notes

Document constraints that explain seemingly poor design choices:

- **[Component] Legacy Constraint**: "This API design is convoluted but must be maintained for backward compatibility with X system"
- **[File/Pattern] Historical Reason**: "Uses outdated pattern Y because of dependency on unmaintained library Z"
- **[Architecture] Migration in Progress**: "Old and new patterns coexist because migration from A to B is 60% complete"

## Positive Findings

- Well-implemented patterns worth highlighting
- Good practices to propagate elsewhere
```

### Actionable Recommendations

For each finding, provide:

1. **Clear classification**: Bug, Security, Performance, Maintainability, Style
2. **Specific location**: File path and line numbers
3. **Concrete fix**: Either "obviously do X" or multiple alternative approaches
4. **Priority rationale**: Why this matters for the project

## Execution Instructions

0. **Initial Reconnaissance**: First, understand what you're reviewing
   - Determine the scope (full codebase, module, file, or function)
   - Get a feel for the structure and organization
   - Identify the type (CLI tool, library, web service, etc.)
   - Note the technologies, frameworks, and languages used
   - Understand the purpose and domain
   - This calibrates which review sections will be relevant

1. Start with sequential high-level analysis
2. Identify all major modules/components
3. Maximize parallelism by spawning multiple Task agents:
   - Create one Task agent per major module/component
   - Create separate Task agents for cross-cutting concerns (security audit, performance analysis, test coverage)
   - Create Task agents for specific file groups (e.g., all config files, all test files, all API endpoints)
   - Each agent performs independent analysis of their assigned scope
   - Launch ALL agents in a single batch for maximum concurrency
   - Agents should analyze: code quality patterns, implementation details, readability
4. While agents are running, continue with any remaining sequential analysis
5. Aggregate findings from all agents as they complete
6. Sort by priority and impact
7. Generate comprehensive report

**Parallelization Strategy**: Identify every independent analysis task possible and run them concurrently. Don't wait for one analysis to complete before starting another if they don't depend on each other.

## Special Focus Areas

Pay special attention to common code quality issues:

- Missing error handling or silent failures
- Unhandled edge cases
- Type safety violations
- Resource leaks or cleanup issues
- Security vulnerabilities
- Performance bottlenecks
- Code duplication
- Complex or unclear logic that could be simplified
- Inconsistent patterns within the codebase

Remember: The goal is not just to find issues but to provide actionable, specific recommendations that improve code quality while following established patterns and conventions.
