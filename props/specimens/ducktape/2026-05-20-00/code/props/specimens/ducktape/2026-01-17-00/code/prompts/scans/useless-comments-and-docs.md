# Scan: Useless Comments and Documentation

## Context

@../shared-context.md

## Core Principle

**Comments and documentation should add value beyond what the code itself expresses.** This applies to:

- Inline comments (`# ...`)
- Block comments (`# ---- Section ----`)
- Docstrings (`"""..."""`)
- Type hints and annotations (should be accurate, not duplicated in docs)

A comment or docstring is useless if it:

- Duplicates information already clear from code structure (types, decorators, names)
- States the obvious ("increment counter" for `counter += 1`, "Validate config" for `validate_config()`)
- Is outdated or contradicts the code
- Uses vague language that doesn't clarify anything

**Good documentation explains WHY, not WHAT.** The code already shows what it does.

## Overview

Python code should be self-documenting through clear naming and structure. Documentation is valuable only when it provides context, rationale, or non-obvious information that cannot be expressed in code.

This scan targets both **comments** and **docstrings** - the same principles apply to both.

---

## Antipattern 1: Duplicating Code Semantics

### BAD: Comment repeats what decorators/types already say

```python
# BAD: Everything in comment is already expressed by code
# Tool: container exec (flat MCP payload, validated via ExecInput)
@mcp.tool(name="exec", flat=True)
async def tool_exec(input: ExecInput, ctx: Context) -> BaseExecResult:
    #              ^^^^^^ type annotation ^^^^  ^^^^^^ return type
    #  ^^^^^^^^ decorator already says it's a tool
    #                     ^^^^ decorator already says flat=True
    """Run a shell command inside the per-session Docker container."""
    ...

# BAD: Comment just restates the type annotation
def process_user(user: User):  # user is a User object
    ...

# BAD: Docstring duplicates function name
def validate_config(config: Config) -> bool:
    """Validate config."""  # Useless! Function name already says this
    ...

# BAD: Docstring duplicates parameter types
def process_user(user: User, admin: bool = False) -> None:
    """Process user.

    Args:
        user: User object to process
        admin: Boolean flag for admin privileges
    """
    # Type annotations already document types! Docs should explain semantics, not types
    ...
```

### GOOD: Comment adds non-obvious context

```python
# GOOD: Explains WHY we use flat=True (not obvious from decorator alone)
@mcp.tool(name="exec", flat=True)  # flat=True exposes ExecInput fields directly to MCP clients
async def tool_exec(input: ExecInput, ctx: Context) -> BaseExecResult:
    """Run a shell command inside the per-session Docker container.

    Uses docker exec for session containers, or ephemeral containers for
    one-off execution. Ephemeral mode avoids state pollution between runs.
    """
    ...

# GOOD: Docstring explains non-obvious validation logic and cross-field constraints
def validate_config(config: Config) -> bool:
    """Validate config structure and cross-field constraints.

    Returns False if optimizer_type="adam" but adam_beta1 is missing,
    or if learning_rate is negative. See docs/config-schema.md for full rules.
    """
    ...

# GOOD: Fix the names first, then docs only for non-obvious details
def update_user_last_seen(user: User, *, from_admin_console: bool = False) -> None:
    """Update last_seen timestamp and log access (with optional admin audit trail).

    Raises SessionExpiredError if user.session has expired.
    """
    # Function name says WHAT it does, param name captures semantic context
    # (why it matters - admin requests get special handling: skip rate limit, audit log)
    # Docs cover edge cases (SessionExpiredError) rather than repeating what code says
    ...
```

---

## Antipattern 2: Obvious Statements

### BAD: Comment states what code already shows

```python
# BAD: Obvious from code
# Increment counter
counter += 1

# BAD: Obvious from variable names
# Get user ID from request
user_id = request.user_id

# BAD: Obvious from control flow
# Check if user exists
if user is not None:
    ...

# BAD: Obvious from function/method names
# Close the client
await client.close()

# BAD: Section header that adds no value
# ---- Helper functions ----
def helper1():
    ...
```

### GOOD: Comment explains non-obvious reasoning

```python
# GOOD: Explains WHY we increment (not obvious)
counter += 1  # Track retries; will abort after 3 (see MAX_RETRIES)

# GOOD: Explains edge case handling
user_id = request.user_id or request.session.get("impersonated_user_id")
# Fall back to session when admin is impersonating another user

# GOOD: Explains why check is necessary
if user is not None:
    # Defensive: user can be None during initial OAuth flow before profile sync
    await user.update_last_seen()

# GOOD: Explains cleanup timing
await client.close()
# Must close before exiting context to flush pending writes to disk
```

---

## Antipattern 3: Outdated or Wrong Comments

### BAD: Comment contradicts code (stale)

```python
# BAD: Comment says "retry 3 times" but code retries 5 times
# Retry up to 3 times on network errors
for attempt in range(5):
    ...

# BAD: Comment references old parameter name
def process_request(request: Request):  # process the req object
    #                         ^^^^^^^ parameter is 'request', not 'req'
    ...

# BAD: TODO that was already done
# TODO: Add validation for email format
def validate_email(email: str) -> bool:
    # Validation logic here (already implemented!)
    return EMAIL_REGEX.match(email) is not None
```

### GOOD: Keep comments in sync or remove them

```python
# GOOD: Accurate comment
# Retry up to 5 times with exponential backoff
for attempt in range(5):
    ...

# GOOD: No misleading comment needed
def process_request(request: Request):
    # Type annotation already documents the parameter
    ...

# GOOD: Remove completed TODOs entirely
def validate_email(email: str) -> bool:
    return EMAIL_REGEX.match(email) is not None
```

---

## Antipattern 4: Vague Comments

### BAD: Comment doesn't clarify anything

```python
# BAD: "Handle the data" - how? why?
# Handle the data
result = transform(data)

# BAD: "Important!" - what's important? why?
# Important!
if config.mode == "production":
    ...

# BAD: "HACK" without explanation
# HACK
time.sleep(0.1)

# BAD: "Fix this" without context
# Fix this later
return None
```

### GOOD: Specific, actionable comments

```python
# GOOD: Explains what transformation and why
# Convert from OpenAI format to internal format (loses reasoning tokens, see #123)
result = transform(data)

# GOOD: Explains why production mode is special
# Production mode disables debug logging and uses encrypted credentials
if config.mode == "production":
    ...

# GOOD: Explains why the hack is necessary
# Workaround for race condition in upstream library (see issue #456)
# Remove when we upgrade to v2.0+ which includes the fix
time.sleep(0.1)

# GOOD: Explains what needs fixing and why
# TODO: Return proper error instead of None (requires client update to handle errors)
return None
```

---

## When Comments ARE Valuable

### ✓ Explaining non-obvious algorithms

```python
# Binary search requires sorted input; we sort once here rather than per-query
data.sort(key=lambda x: x.timestamp)
```

### ✓ Documenting edge cases

```python
# Empty list is valid (represents "no filters"), but None means "use defaults"
if filters is None:
    filters = DEFAULT_FILTERS
```

### ✓ Referencing external context

```python
# Matches OpenAI API behavior: trailing newlines are stripped (see docs/api.md#text-normalization)
return text.rstrip('\n')
```

### ✓ Warning about gotchas

```python
# WARNING: Modifies input list in-place for performance (avoids copy)
def deduplicate(items: list) -> list:
    ...
```

### ✓ Explaining temporary workarounds

```python
# Temporary: remove this when upstream PR #789 is merged and we upgrade
if isinstance(response, LegacyFormat):
    response = convert_to_new_format(response)
```

### ✓ Documenting WHY, not WHAT

```python
# Use thread pool instead of process pool because:
# 1. Data is already in memory (no serialization overhead)
# 2. Tasks are I/O-bound (network calls), not CPU-bound
with ThreadPoolExecutor() as executor:
    ...
```

---

## Detection Strategy

**Goal**: Find ALL useless comments for manual review (100% recall target).

**Approach**: Low-precision, high-recall extraction of ALL comments, then manual filtering.

### Phase 1: Extract ALL Python Comments

```python
import ast
import re
from pathlib import Path

def extract_all_comments(file_path: Path) -> list[dict]:
    """Extract every comment from a Python file with surrounding context.

    Returns list of dicts with:
        - line_num: Line number of comment
        - comment: Comment text (without # prefix)
        - context_before: 3 lines before comment
        - context_after: 3 lines after comment
        - comment_type: "inline" | "block" | "docstring"
    """
    with open(file_path) as f:
        lines = f.readlines()

    comments = []

    # Extract regular comments (# ...)
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith('#') and not stripped.startswith('#!'):
            comment_text = stripped.lstrip('#').strip()
            context_before = lines[max(0, i-4):i-1]
            context_after = lines[i:min(len(lines), i+3)]

            comments.append({
                'line_num': i,
                'comment': comment_text,
                'context_before': ''.join(context_before),
                'context_after': ''.join(context_after),
                'comment_type': 'inline' if '#' in line[:line.index('#')] else 'block'
            })

    # Extract docstrings via AST
    try:
        tree = ast.parse(''.join(lines), filename=str(file_path))
        for node in ast.walk(tree):
            docstring = ast.get_docstring(node)
            if docstring and hasattr(node, 'lineno'):
                line_num = node.lineno
                context_before = lines[max(0, line_num-4):line_num-1]
                context_after = lines[line_num:min(len(lines), line_num+10)]

                comments.append({
                    'line_num': line_num,
                    'comment': docstring,
                    'context_before': ''.join(context_before),
                    'context_after': ''.join(context_after),
                    'comment_type': 'docstring'
                })
    except SyntaxError:
        pass  # Skip files with syntax errors

    return comments


def scan_codebase_comments(root_dir: Path) -> dict[str, list[dict]]:
    """Scan all Python files and extract comments with context.

    Returns dict mapping file paths to lists of comment dicts.
    """
    all_comments = {}
    for py_file in root_dir.rglob('*.py'):
        if 'venv' in py_file.parts or '__pycache__' in py_file.parts:
            continue
        comments = extract_all_comments(py_file)
        if comments:
            all_comments[str(py_file)] = comments
    return all_comments


# Example usage:
# comments = scan_codebase_comments(Path('.'))
# for file_path, file_comments in comments.items():
#     print(f"\n{file_path}:")
#     for c in file_comments:
#         print(f"  Line {c['line_num']} [{c['comment_type']}]: {c['comment'][:60]}...")
```

### Phase 2: Automated Filtering (Low Precision)

```python
def is_likely_useless(comment_dict: dict) -> bool | None:
    """Check if comment is likely useless (automated heuristics).

    Returns:
        True: Likely useless (high confidence)
        False: Likely useful (high confidence)
        None: Unclear, requires manual review
    """
    comment = comment_dict['comment'].lower()
    context_after = comment_dict['context_after'].lower()

    # High-confidence useless patterns
    useless_patterns = [
        # Duplicating type annotations
        (r'(\w+) is a (\w+)', lambda: 'def ' in context_after and ':' in context_after),
        # Obvious increments/decrements
        (r'increment|decrement', lambda: '++' in context_after or '+= 1' in context_after or '-= 1' in context_after),
        # Section headers with no semantic value
        (r'^-+\s*(helper|utility|internal|private)\s*(functions?|methods?|classes?)\s*-+$', lambda: True),
        # Empty TODOs
        (r'^todo:?\s*$', lambda: True),
    ]

    for pattern, check_context in useless_patterns:
        if re.search(pattern, comment) and check_context():
            return True

    # High-confidence useful patterns
    useful_patterns = [
        r'\bwhy\b',  # Explains reasoning
        r'\bworkaround\b',  # Temporary fix
        r'\bhack\b.*\bbecause\b',  # Explained hack
        r'\bwarning\b',  # Warning about gotchas
        r'\bedge case\b',  # Documents edge cases
        r'see (issue|pr|docs?).*#\d+',  # References external context
        r'todo:.*\([^)]+\)',  # TODO with context/assignee
    ]

    for pattern in useful_patterns:
        if re.search(pattern, comment):
            return False

    # Unclear - requires manual review
    return None
```

### Automated Scan Commands

```bash
# Find all comments (including inline comments)
rg --type py '^[^#]*#' --line-number

# Find block comments (lines starting with #)
rg --type py '^\s*#' --line-number

# Find TODO comments
rg --type py '\bTODO\b' --line-number --ignore-case

# Find comments that might duplicate type annotations
rg --type py '#.*\b(is a|is an|type of|instance of)\b' --line-number --ignore-case

# Find section header comments
rg --type py '^\s*#\s*-+.*-+\s*$' --line-number
```

### Manual Review Process (Required)

Since this is a low-precision scan, **manual review is mandatory**:

1. **Extract ALL comments** with ±3 line context (using script above)
2. **Review each comment** in context:
   - Does it add information beyond code structure?
   - Does it explain WHY, not just WHAT?
   - Is it accurate and up-to-date?
   - Could it be replaced by better naming/refactoring?
3. **Categorize**:
   - USELESS: Remove entirely
   - OUTDATED: Update or remove
   - VAGUE: Make specific or remove
   - USEFUL: Keep (explains WHY, edge cases, non-obvious behavior)
4. **Refactor** before removing:
   - If comment explains unclear code, improve the code first
   - Extract magic numbers to named constants
   - Rename variables to be self-documenting

---

## Fix Strategy

### Priority 1: Remove Duplicates

```python
# Before
# Tool: container exec (flat MCP payload, validated via ExecInput)
@mcp.tool(name="exec", flat=True)
async def tool_exec(input: ExecInput, ctx: Context) -> BaseExecResult:
    ...

# After - decorator and types speak for themselves
@mcp.tool(name="exec", flat=True)
async def tool_exec(input: ExecInput, ctx: Context) -> BaseExecResult:
    """Run a shell command inside the per-session Docker container."""
    ...
```

### Priority 2: Update Outdated Comments

```python
# Before - WRONG
# Retry up to 3 times on network errors
for attempt in range(5):
    ...

# After - CORRECTED
# Retry up to 5 times on network errors
for attempt in range(5):
    ...
```

### Priority 3: Make Vague Comments Specific

```python
# Before - VAGUE
# Handle the data
result = transform(data)

# After - SPECIFIC
# Convert from OpenAI response format to internal BaseExecResult format
result = transform(data)
```

### Priority 4: Remove Obvious Statements

```python
# Before
# Increment counter
counter += 1

# After - no comment needed
counter += 1
```

## Output Format for Manual Review

Generate a review document with all comments, including their context (+/- 3 lines) and content.

## Examples from Codebase

### Duplicating Decorator Semantics

```python
# ✗ BEFORE: Comment duplicates decorator + type annotations
# Tool: container exec (flat MCP payload, validated via ExecInput)
@mcp.tool(name="exec", flat=True)
async def tool_exec(input: ExecInput, ctx: Context) -> BaseExecResult:
    """Run a shell command inside the per-session Docker container."""
    ...

# ✓ AFTER: Decorator and types are self-documenting
@mcp.tool(name="exec", flat=True)
async def tool_exec(input: ExecInput, ctx: Context) -> BaseExecResult:
    """Run a shell command inside the per-session Docker container."""
    ...
```

### Obvious Statements

```python
# ✗ BEFORE: Obvious from method name
# Close the client
await client.close()

# ✓ AFTER: Only comment if non-obvious
await client.close()  # Must close before exit to flush pending writes
```

---

## References

- [PEP 8 - Comments](https://peps.python.org/pep-0008/#comments)
- [Google Python Style Guide - Comments and Docstrings](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings)
- [Code Complete - Self-Documenting Code](https://www.oreilly.com/library/view/code-complete-2nd/0735619670/)
