**Canonical slugs** for common issues (use these exact names when the issue clearly fits):

Note: If an issue doesn't exactly match a canonical slug, or combines multiple things, or has snapshot-specific context that makes a different name clearer, use a descriptive name instead. Don't force-fit.

- `walrus.yaml` - walrus operator (:=) opportunities
- `imports-at-top.yaml` - imports not at module top (PEP 8)
- `inline-trivial-var.yaml` - unnecessary intermediate **variables** (NOT functions - use `trivial-wrapper.yaml` for functions)
- `dead-code.yaml` - unused/unreachable code
- `unused-params.yaml` - function parameters declared but ignored in body
- `dup-*.yaml` - code duplication (use `dup-` prefix, e.g., `dup-pagination.yaml`)
- `misplaced-default.yaml` - default value at wrong layer (inner layer has default that should only exist at entrypoint/CLI, or duplicates higher-level default)
- `early-bailout.yaml` - nested if-block should use early return/bailout pattern for flatter control flow
- `trivial-wrapper.yaml` - function so trivial it should be inlined (e.g., `fn x(a) { return a.b; }`); not for functions serving as decoupling layers
- `useless-fast-path-check.yaml` - guard clause that returns early for empty input/no-op case, but the subsequent code already handles that case naturally (e.g., empty loop). Remove unless the skipped body is genuinely expensive
