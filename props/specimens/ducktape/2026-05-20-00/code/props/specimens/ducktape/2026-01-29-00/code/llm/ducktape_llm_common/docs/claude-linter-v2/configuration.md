# Claude Linter v2 Configuration

Claude Linter v2 uses a modular configuration format that provides fine-grained control over individual checks and rules.

## Benefits of Modular Configuration

1. **Fine-grained control**: Each check gets its own configuration section
2. **Better organization**: Related settings are grouped together
3. **Extensibility**: Easy to add custom checks without modifying the schema
4. **Self-documenting**: Each check can have its own message and severity

## Configuration Format

Each check gets its own configuration section with standard fields:

```toml
[python.bare_except]
enabled = true
message = "Bare except clauses hide errors and make debugging difficult"
severity = "error"

[python.getattr]
enabled = true
message = "getattr() usage is discouraged - use proper attribute access"
severity = "error"

[ruff.E722]
enabled = true
message = "Do not use bare except, specify exception types"
autofix = false
```

## Configuration Structure

### Python Checks

Each Python AST check gets its own section:

```toml
[python.bare_except]
enabled = true
message = "Custom error message"
severity = "error"  # or "warning", "info"

[python.hasattr]
enabled = true
message = "hasattr() usage is discouraged"

[python.getattr]
enabled = true
message = "getattr() usage is discouraged"

[python.setattr]
enabled = true
message = "setattr() usage is discouraged"

[python.barrel_init]
enabled = true
message = "Barrel __init__.py files create circular dependencies"
```

### Ruff Rules

Each ruff rule can be individually configured:

```toml
[ruff.E722]
enabled = true
message = "Do not use bare except"
autofix = false
severity = "error"

[ruff.B009]
enabled = true
message = "Do not call getattr with a constant attribute value"
autofix = false

# Add any ruff rule dynamically
[ruff.C901]
enabled = false  # Disable complexity check by default
message = "Function is too complex"
severity = "warning"
```

### Test File Configuration

Specify which rules to relax for test files:

```toml
test_patterns = [
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**/*.py"
]

test.relaxed_rules = [
    "python.bare_except",
    "ruff.E722",
    "ruff.B904"
]
```

## File Naming

Claude Linter v2 looks for configuration files in the following order:

1. `.claude-linter.toml`
2. `.claude-linter-v2.toml`
3. `claude-linter.toml`

The search starts in the current directory and moves up to parent directories.

## Example Modular Configuration

Here's a complete example:

```toml
# Claude Linter v2 Modular Configuration
version = "2.0"

# Display settings
max_errors_to_show = 5

# Path-based access control
[[access_control]]
paths = ["production/**", "*.prod.py"]
tools = ["Write", "Edit", "MultiEdit"]
action = "deny"
message = "Production files are read-only"

# Python checks
[python.bare_except]
enabled = true
message = "Bare except clauses hide errors"
severity = "error"

[python.hasattr]
enabled = true
message = "Use isinstance() instead of hasattr()"
severity = "error"

[python.getattr]
enabled = true
message = "Use direct attribute access"
severity = "error"

[python.setattr]
enabled = true
message = "Use direct attribute assignment"
severity = "error"

[python.barrel_init]
enabled = true
message = "Keep __init__.py files minimal"
severity = "error"

# Python tools
python.tools = ["ruff", "mypy"]

# Ruff rules (showing a few examples)
[ruff.E722]
enabled = true
message = "Do not use bare except"
autofix = false

[ruff.S113]
enabled = true
message = "Probable use of requests call without timeout"
severity = "error"

[ruff.B008]
enabled = true
message = "Do not perform function calls in argument defaults"
autofix = true

# Hook behaviors
[hooks.pre]
auto_fix = false

[hooks.post]
auto_fix = true
autofix_categories = ["formatting"]
inject_permissions = true

[hooks.stop]
auto_fix = false
inject_permissions = false
quality_gate = true

# Test configuration
test_patterns = [
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**/*.py"
]

test.relaxed_rules = [
    "python.bare_except",
    "ruff.E722"
]

# Task profiles
[[profiles]]
name = "refactoring"
description = "Python refactoring with git and tests"
predicate = 'Edit("**/*.py") and Bash("git:*") and Bash("pytest:*")'
duration = "4h"
```

## Adding Custom Checks

The modular format makes it easy to add custom checks:

```toml
# Custom mypy checks
[mypy.no_untyped_def]
enabled = true
message = "All functions must have type annotations"
severity = "warning"

# Custom project-specific checks
[project.no_print_statements]
enabled = true
message = "Use logging instead of print statements"
severity = "warning"
```
