# Python-First Predicate Design for Claude Linter v2

## Core Concept: Python Functions as First-Class Rules

Since Python provides the ultimate flexibility, let's embrace it while making common cases ergonomic.

## Basic Design

```python
# In .claude-linter.py (yes, Python config file!)

from claude_linter import rule, Context, allow, deny, warn

# Simple function predicates
@rule
def no_prod_edits(ctx: Context):
    """Prevent edits to production files"""
    if ctx.tool in ['Edit', 'Write'] and 'prod' in ctx.path:
        return deny("Production files are read-only")
    return allow()

# Using decorators for common patterns
@rule.glob("src/**/*.py", tools=["Edit", "Write"])
def python_files_ok(ctx: Context):
    """Allow editing Python source files"""
    return allow()

# Time-based rules
@rule
def business_hours_check(ctx: Context):
    """Restrict production changes during business hours"""
    from datetime import datetime
    now = datetime.now()
    if 9 <= now.hour < 17 and ctx.path.startswith('prod/'):
        return deny("No production changes during business hours")
    return allow()

# Complex multi-condition logic
@rule
def migration_safety(ctx: Context):
    """Ensure database migrations are reversible"""
    if ctx.tool == 'Write' and 'migrations' in ctx.path:
        if 'def down(' not in ctx.content:
            return deny("Migrations must include a down() method")
    return allow()

# Using external data
@rule
async def pr_approval_check(ctx: Context):
    """Check PR has required approvals"""
    if 'src/core' in ctx.path:
        pr_data = await ctx.get_pr_data()  # Built-in helper
        if pr_data.approvals < 2:
            return deny(f"Core changes need 2 approvals, have {pr_data.approvals}")
    return allow()

# Composing rules
@rule
def combined_check(ctx: Context):
    """Apply multiple checks"""
    # Can call other rules
    if (result := no_prod_edits(ctx)).denied:
        return result
    if (result := business_hours_check(ctx)).denied:
        return result
    return allow()
```

## Making Common Cases Easy

```python
# Prebuilt rule factories
from claude_linter import rules

# Glob patterns
allow_src = rules.glob_allow("src/**/*.py", tools=["Edit", "Write"])
deny_generated = rules.glob_deny("**/*.generated.*")

# Git commands
safe_git = rules.git_safe_commands()
no_force_push = rules.git_deny(["push --force", "reset --hard"])

# Time windows
business_hours = rules.time_window(days="weekdays", hours="9-17", tz="US/Pacific")

# Combine with custom logic
@rule
def custom_with_prebuilt(ctx: Context):
    # Use prebuilt rules
    if safe_git(ctx).allowed:
        return allow()

    # Add custom logic
    if ctx.command.startswith('git push'):
        if '--force' in ctx.command and ctx.user == 'admin':
            return allow()  # Admin override

    return deny("Unsafe git command")
```

## Natural Language Descriptions

```python
@rule
def check_security_patterns(ctx: Context):
    """
    Prevent common security issues:
    - No hardcoded passwords
    - No eval() or exec()
    - No SQL injection vulnerabilities
    """
    if ctx.tool != 'Edit':
        return allow()

    issues = []

    if 'password = "' in ctx.content:
        issues.append("Hardcoded password detected")

    if 'eval(' in ctx.content or 'exec(' in ctx.content:
        issues.append("Unsafe eval/exec usage")

    if issues:
        return deny(f"Security issues: {', '.join(issues)}")

    return allow()

# The docstring becomes the natural language description!
print(check_security_patterns.description)
# "Prevent common security issues: No hardcoded passwords, No eval() or exec(), No SQL injection vulnerabilities"
```

## TOML Configuration for Simple Cases

For users who prefer configuration over code:

```toml
# .claude-linter.toml

# Simple patterns (converted to Python rules internally)
[[rules]]
glob = "src/**/*.py"
action = "allow"
tools = ["Edit", "Write"]

[[rules]]
glob = "**/*.generated.*"
action = "deny"
message = "Generated files should not be edited"

# Reference Python functions
[[rules]]
function = "my_rules.check_migrations"  # Import from my_rules.py

# Inline Python for one-offs
[[rules]]
python = """
def check(ctx):
    if ctx.tool == 'Bash' and 'sudo' in ctx.command:
        return deny('No sudo commands allowed')
    return allow()
"""
```

## Session Rules via CLI

```bash
# Add a Python predicate for this session
claude-linter session add --python "
def check(ctx):
    if ctx.path.startswith('experimental/'):
        return allow()  # Temporary experiment
    return None  # Defer to other rules
"

# Or use a one-liner
claude-linter session add --expr "ctx.tool == 'Edit' and ctx.path.endswith('.md')"

# Or load from file
claude-linter session add --file ./temp_rules.py
```

## Safety and Sandboxing

```python
# Rules run in a restricted environment
class RestrictedContext:
    """Context provided to rule functions"""

    # Safe attributes
    tool: str
    path: str
    content: str
    old_content: str
    command: str
    session_id: str

    # Safe methods
    def glob_match(self, pattern: str) -> bool: ...
    def get_pr_data(self) -> PRData: ...
    def get_file_history(self) -> list[Change]: ...

    # NOT available: open(), __import__, eval, exec, etc.
```

## Rule Debugging and Explanation

```python
@rule
def complex_rule(ctx: Context):
    """Check various conditions for code quality"""

    # Rules can log their reasoning
    ctx.log("Checking file type")
    if not ctx.path.endswith('.py'):
        ctx.log("Not a Python file, allowing")
        return allow()

    ctx.log(f"Python file: {ctx.path}")

    if 'test' in ctx.path:
        ctx.log("Test file, relaxing rules")
        return allow()

    if 'hasattr' in ctx.content:
        ctx.log("Found hasattr usage")
        return deny("Use proper type checking instead of hasattr")

    return allow()

# When rule fires, logs are included:
# "Denied by complex_rule: Use proper type checking instead of hasattr
#  Debug log:
#  - Checking file type
#  - Python file: src/main.py
#  - Found hasattr usage"
```

## Benefits of Python-First Approach

1. **Maximum Power**: Any logic is possible
2. **Familiar**: Python developers already know the language
3. **Testable**: Rules are just functions, easy to unit test
4. **Debuggable**: Can use print, logging, debugger
5. **Reusable**: Rules can import and compose
6. **Type Safe**: With type hints and checking
7. **Discoverable**: Docstrings provide documentation

## Example: Real-World Rule Set

```python
# rules.py
from claude_linter import rule, Context, allow, deny, warn
from datetime import datetime
import re

# Simple protections
@rule.glob("**/*.secret", action="deny")
@rule.glob("**/.env", action="deny")
def protect_secrets(ctx): pass

# Smart git safety
@rule
def git_safety(ctx: Context):
    """Allow safe git commands, block dangerous ones"""
    if ctx.tool != 'Bash' or not ctx.command.startswith('git'):
        return allow()

    dangerous = ['push --force', 'reset --hard', 'clean -fd']
    for pattern in dangerous:
        if pattern in ctx.command:
            # Check for override
            if ctx.session.has_override('force_git'):
                return warn(f"Dangerous git command: {pattern}")
            return deny(f"Dangerous git command: {pattern}")

    return allow()

# Context-aware Python rules
@rule
def python_quality(ctx: Context):
    """Enforce Python code quality standards"""
    if not ctx.path.endswith('.py') or ctx.tool not in ['Edit', 'Write']:
        return allow()

    # Parse with AST
    try:
        import ast
        tree = ast.parse(ctx.content)
    except SyntaxError as e:
        return deny(f"Python syntax error: {e}")

    # Check for issues
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            return deny(f"Bare except at line {node.lineno}")

        if isinstance(node, ast.Call):
            if getattr(node.func, 'id', None) in ['hasattr', 'getattr']:
                if 'test' not in ctx.path:  # Relax for tests
                    return deny(f"Use proper type checking instead of {node.func.id}")

    return allow()

# Time and user based
@rule
def deployment_window(ctx: Context):
    """Only allow deployments during safe hours"""
    if 'deploy' not in ctx.path and 'k8s' not in ctx.path:
        return allow()

    hour = datetime.now().hour
    if 9 <= hour <= 16:  # 9 AM - 4 PM
        return allow()

    if ctx.session.user in ['oncall', 'admin']:
        return warn("Deployment outside hours - be careful!")

    return deny("Deployments only allowed 9 AM - 4 PM")
```

## Integration with LLM

```python
@rule
async def llm_code_review(ctx: Context):
    """Use LLM for nuanced code review"""
    if ctx.tool != 'Edit' or not ctx.path.endswith('.py'):
        return allow()

    # Only for non-trivial changes
    if len(ctx.content) - len(ctx.old_content) < 50:
        return allow()

    review = await ctx.llm_review(
        prompt=f"Review this Python code change for issues",
        model="gpt-4o-mini"
    )

    if review.has_issues:
        return warn(f"LLM review: {review.summary}")

    return allow()
```

This Python-first approach gives us maximum flexibility while still being approachable for common cases!
