# Verbose Documentation Detector

Find documentation and comments that restate what code already says, add no insight, or prescribe behavior instead of describing affordances.

## What to Flag

- **Restating docstrings**: Docstrings that just repeat the function name, parameter names, or return type without adding insight
- **Restating comments**: Comments that describe what the next line does when the code is self-explanatory
- **Parameter echoing**: Args sections that just list parameter names and types already in the signature
- **Returns echoing**: Returns sections that restate the return type annotation
- **Trivial class docstrings**: Docstrings like "A class that represents X" where X is the class name
- **Prescriptive docstrings**: Docstrings that describe what callers should do instead of what the code does/provides
- **Historical comments**: Comments about removed code, old behavior, or "used to be X"
- **Section banners**: `# ========== Section ==========` comments that add visual noise without information
- **Changelog comments**: `# Added in v1.2` or `# Modified 2024-01-15` that belong in version control

## What NOT to Flag

- **TODOs/FIXMEs**: Valid work items belong in code, not just issue trackers
- **Useful module-level docstrings**: Those that concisely summarize the file's purpose when not redundant with other docs
- **Non-obvious behavior docs**: Edge cases, error conditions, invariants, contracts
- **Why comments**: Comments explaining rationale, not what the code does
- **External context docs**: Comments/docstrings explaining why something exists, how it integrates into the broader system, or its role in architecture not obvious from local context
- **Disambiguation docs**: Docstrings that clarify ambiguous naming (e.g., "container-side path" vs "host-side path", "UTC timestamp" vs "local time"). If the name alone could be misinterpreted, the documentation adds value. Alternative: rename to be unambiguous (e.g., `working_dir` → `container_working_dir`)
- **Test intent comments**: Comments in tests that describe what specific edge case, subtlety, or behavior the test is verifying. These clarify the test's purpose beyond what the test name conveys and help future readers understand why the test exists

## Method

### Exhaustive Extraction (Required First Step)

**You MUST explicitly extract and enumerate ALL docstrings and comments before analysis.**

1. **Extract all docstrings**: Use `rg -n '"""' /workspace` and read each one
2. **Extract all comments**: Use `rg -n "^\s*#" /workspace` and read each one
3. **List them explicitly**: Write out each docstring/comment with its location before evaluating

This prevents skipping items. Do not rely on "scanning" — explicitly list every piece of documentation.

### Analysis Strategy

1. **Read code structure first** - Understand what the code does before evaluating its documentation
2. **Compare doc to signature** - Check if docstring adds anything beyond what signature/types show
3. **Check comment necessity** - Would removing the comment lose any information?

### Process Steps

1. **Extract all docstrings** in functions, methods, classes, modules — list each with file:line
2. **For each docstring** (go through your extracted list one by one):
   - Compare to function name and signature
   - Does it explain WHY or just restate WHAT?
   - Does Args section add anything beyond type hints?
3. **Extract all comments** (lines starting with `#`) — list each with file:line
4. **For each comment** (go through your extracted list one by one):
   - Read the following code
   - Would the code be unclear without the comment?
   - Is the comment describing what vs why?

## Positive Examples (Acceptable Documentation)

Docstring explaining non-obvious behavior:

```python
def retry_with_backoff(fn: Callable[[], T], max_attempts: int = 3) -> T:
    """Retries fn with exponential backoff (1s, 2s, 4s). Raises last exception after max_attempts."""
    ...
```

Comment explaining WHY:

```python
# Use UTC to avoid DST edge cases in scheduling
scheduled_at = datetime.now(timezone.utc)
```

Docstring describing invariants/contracts:

```python
def transfer(from_account: Account, to_account: Account, amount: Decimal) -> None:
    """Atomically transfers amount. Raises InsufficientFunds if from_account.balance < amount."""
    ...
```

Docstring disambiguating an ambiguous name:

```python
# GOOD: docstring clarifies what "working_dir" means
@property
def working_dir(self) -> Path:
    """Container-side path (not host path)."""
    return WORKING_DIR

# ALSO GOOD: unambiguous name, no docstring needed
@property
def container_working_dir(self) -> Path:
    return WORKING_DIR
```

## Negative Examples (Flag These)

Restating the function name:

```python
def get_user(user_id: str) -> User:
    """Get a user by ID.

    Args:
        user_id: The user ID.

    Returns:
        The user.
    """
    ...
```

Restating what code does:

```python
# Increment the counter
counter += 1

# Check if user is None
if user is None:
    return None
```

Prescriptive instead of descriptive:

```python
class DatabaseConfig:
    """Use this class to configure the database connection.

    First, create an instance with your connection parameters.
    Then, call connect() to establish the connection.
    Finally, use execute() to run queries.
    """
    ...
```

Section banners with no content:

```python
# ============================================================================
# Helper Functions
# ============================================================================

def helper_one():
    ...
```

Args that echo signature:

```python
def process_data(data: list[dict], strict: bool = False) -> ProcessResult:
    """Process data records.

    Args:
        data: The data to process.
        strict: Whether to use strict mode.

    Returns:
        ProcessResult: The result of processing.
    """
```

## Decision Heuristics

- **Delete test**: If removing the doc/comment loses zero information, flag it
- **Signature coverage**: If signature + types tell the whole story, docstring is redundant
- **Why vs what**: Comments explaining "why" are valuable; comments describing "what" are usually redundant
- **Non-obvious behavior**: Keep docs that explain edge cases, error conditions, or non-intuitive behavior
- **API boundaries**: Public API docs may justify more verbosity; internal code should be minimal

## Command Snippets

```bash
# Find docstrings
rg -n '"""' /workspace

# Find comment lines
rg -n "^\s*#" /workspace

# Find Args/Returns sections
rg -n "Args:|Returns:|Parameters:|Return:" /workspace

# Find section banners
rg -n "^# [=\-]{5,}" /workspace
```

## Notes

- Focus on production code; test file docstrings may have different standards
- Consider project conventions - some codebases require docstrings for all public functions
