# Claude Linter v2 Predicate System Design Options

## Design Goals

- User-friendly for common cases
- Powerful enough for complex logic
- Self-documenting (can explain what it does)
- Safe (no arbitrary code execution)
- Composable (combine simple predicates into complex ones)

## Option 1: Multi-Type Predicate System

```toml
# Simple glob patterns (most common case)
[[rules]]
predicate = { glob = "src/**/*.py", action = "allow" }
tools = ["Edit", "Write"]

# Exclude pattern
[[rules]]
predicate = { glob = "**/*.generated.py", action = "deny" }

# Safe git commands (built-in predicate)
[[rules]]
predicate = { builtin = "safe_git_commands" }

# Time-based rule
[[rules]]
predicate = {
  all_of = [
    { glob = "prod/**" },
    { time = "business_hours" }
  ]
}
action = "deny"
message = "No production edits during business hours"

# Complex logic with Python expression
[[rules]]
predicate = {
  expr = "tool == 'Bash' and command.startswith('rm') and not is_test_file(path)"
}
action = "deny"

# Full Python function (rare cases)
[[rules]]
predicate = {
  python = """
def check(ctx):
    # Complex logic here
    if ctx.tool == 'Edit' and 'migration' in ctx.path:
        # Check if migration is reversible
        content = ctx.new_content
        if 'down()' not in content:
            return 'deny', 'Migrations must be reversible'
    return 'allow'
"""
}
```

### Natural Language Generation

```python
def to_natural_language(predicate):
    if 'glob' in predicate:
        return f"files matching {predicate['glob']}"
    elif 'builtin' in predicate:
        return BUILTIN_DESCRIPTIONS[predicate['builtin']]
    elif 'all_of' in predicate:
        parts = [to_natural_language(p) for p in predicate['all_of']]
        return " AND ".join(parts)
    # etc.
```

## Option 2: Typed Predicate Classes

```yaml
rules:
  - predicate:
      type: file_pattern
      pattern: "src/**/*.py"
      exclude: ["**/*.generated.py", "**/*.test.py"]
    action: allow
    tools: [Edit, Write]

  - predicate:
      type: git_command
      allow: [status, diff, log, add, commit]
      deny: [push --force, reset --hard]

  - predicate:
      type: time_window
      days: [mon, tue, wed, thu, fri]
      hours: "9:00-17:00"
      timezone: "US/Pacific"
    action: deny
    message: "No edits during business hours"

  - predicate:
      type: composite
      operator: and
      predicates:
        - type: file_pattern
          pattern: "database/migrations/**"
        - type: content_check
          must_contain: ["def down(", "def rollback("]
    message: "Migrations must be reversible"
```

## Option 3: Lisp-like S-expressions

```toml
[[rules]]
predicate = '(and (glob "src/**/*.py") (not (glob "**/*.test.py")))'
action = "allow"

[[rules]]
predicate = '(or (git-command "push --force") (git-command "reset --hard"))'
action = "deny"

[[rules]]
predicate = '''
(and
  (= tool "Edit")
  (matches path ".*\.py$")
  (contains new-content "hasattr"))
'''
action = "deny"
message = "No hasattr in Python files"
```

## Option 4: Rule Builder DSL

```toml
[[rules]]
predicate = "files matching 'src/**/*.py' except tests"
action = "allow"

[[rules]]
predicate = "bash commands starting with 'rm' in production files"
action = "deny"

[[rules]]
predicate = "edits to core/* during business_hours"
action = "deny"

# With variables
[[rules]]
predicate = """
when tool is Edit
and path matches {production_paths}
and time is business_hours
then deny with "No production edits during business hours"
"""

[variables]
production_paths = ["prod/**", "deploy/**", "*.prod.py"]
```

## Option 5: Hybrid with Smart Defaults

```toml
# Simple string = glob pattern (most common)
[[rules]]
predicate = "src/**/*.py"
action = "allow"
tools = ["Edit"]

# Object = structured predicate
[[rules]]
predicate = { git = { deny = ["force-push", "hard-reset"] } }

# Tagged template for complex logic
[[rules]]
predicate = { expr = "Edit('src/**') and not test_file and business_hours" }

# Python function with sandboxed execution
[[rules]]
predicate = {
  fn = "check_migration_safety",  # References functions.toml
  sandbox = true
}

# Natural language (compiled to predicate)
[[rules]]
predicate = {
  natural = "allow editing Python files in src/ except during deployments"
}
```

## Recommendation: Layered Approach

```toml
# Layer 1: Simple patterns (80% of use cases)
[[rules]]
pattern = "src/**/*.py"  # Just a glob
tools = ["Edit", "Write"]
action = "allow"

# Layer 2: Built-in predicates
[[rules]]
predicate = "safe_git_commands"  # String references built-in
action = "allow"

# Layer 3: Expression language (10% of cases)
[[rules]]
predicate = { expr = "Edit('src/**') and not Edit('**/*.test.py')" }
action = "allow"

# Layer 4: Structured predicates
[[rules]]
predicate = {
  type = "composite",
  all = [
    { file = "database/migrations/**" },
    { content = { contains = "def down(" } }
  ]
}
message = "Migrations must have rollback"

# Layer 5: Python functions (1% of cases, requires extra config)
[functions.check_pr_approval]
sandbox = true
code = """
def check(ctx):
    # Can call allowed external APIs
    pr_data = get_pr_status(ctx.git_branch)
    if pr_data['reviews'] < 2:
        return 'deny', 'PR needs 2 reviews'
    return 'allow'
"""

[[rules]]
predicate = { function = "check_pr_approval" }
apply_to = ["prod/**"]
```

## Natural Language Interface

Each predicate type can describe itself:

```python
class Predicate:
    def describe(self) -> str:
        """Human-readable description"""

    def explain_decision(self, context, result) -> str:
        """Why did this rule match/not match?"""

# Examples:
"Allow editing Python files in src/ except tests"
"Deny force push and hard reset git commands"
"Block edits to production files during business hours (9-5 PST)"

# Explanations:
"Denied because file 'prod/api.py' matches pattern 'prod/**' and current time (10:30) is within business hours"
```

## Key Design Decisions

1. **Progressive complexity**: Simple cases are simple, complex cases are possible
2. **Multiple representations**: Users can pick what feels natural
3. **Self-documenting**: Can always explain what a rule does and why it fired
4. **Safe by default**: Python code runs in sandbox with limited capabilities
5. **Composable**: Build complex rules from simple pieces

## User Experience Examples

### Beginner: Just wants to protect some files

```toml
[[rules]]
pattern = "*.prod.py"
action = "deny"
message = "Production files are read-only"
```

### Intermediate: Wants time-based rules

```toml
[[rules]]
predicate = "business_hours"
pattern = "prod/**"
action = "deny"
```

### Advanced: Complex multi-condition logic

```toml
[[rules]]
predicate = {
  expr = """
    (Edit('src/**/*.py') or Write('src/**/*.py')) and
    not (test_file or fixture_file) and
    not has_approval('tech-lead')
  """
}
action = "warn"
message = "Consider getting tech lead approval for core changes"
```

### Expert: Custom Python logic

```toml
[functions.semantic_check]
code = """
def check(ctx):
    if ctx.tool != 'Edit':
        return 'allow'

    # Use tree-sitter to parse
    tree = parse_python(ctx.new_content)

    # Check for specific patterns
    if has_raw_sql_queries(tree) and not has_sql_injection_protection(tree):
        return 'deny', 'SQL queries must use parameterized statements'

    return 'allow'
"""
```
