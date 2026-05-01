@../../../AGENTS.md

# Debundler Implementation Constraints

## AST Requirement

JavaScript transformation work must use proper AST-based operations on the
SWC-parsed input. Do not use raw text rewrites, string scanning, regex
rewriting, ad hoc source patching, or other text-based mutation as a
substitute for AST transformations.

## Working Rule

If a proposed change improves a test result without improving real
correctness, do not make that change. If the easiest fix is not the deepest
correct fix, do the deeper correct fix or stop and explain the blocker.
