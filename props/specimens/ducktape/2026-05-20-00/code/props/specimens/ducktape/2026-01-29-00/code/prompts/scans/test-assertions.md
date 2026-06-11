# Scan: Test Assertion Antipatterns

## Context

@../shared-context.md

## Overview

Tests using plain `assert` statements miss opportunities for better error messages, expressivity, and composability that PyHamcrest matchers provide.

## Core Principle

**Use PyHamcrest matchers for better test assertions**:

- More expressive: `has_properties(status="success")` vs `assert obj.status == "success"`
- Better error messages: "Expected: object with property 'status' equal to 'success', but was 'failed'"
- Composable: Combine matchers with `all_of()`, `any_of()`, `not_()`
- Concise for complex assertions

## Pattern 1: Field-by-Field Assertions

### BAD: Manual field-by-field assertions

```python
def test_parse_numstat_output():
    numstat = "10\t5\tsrc/main.py\n3\t0\tREADME.md\n-\t-\timage.png"
    changes = parse_numstat_output(numstat)

    # BAD: Verbose, repetitive, fragile
    assert len(changes) == 3
    assert changes[0].path == "src/main.py"
    assert changes[0].additions == 10
    assert changes[0].deletions == 5
    assert changes[0].is_binary is False

    assert changes[1].path == "README.md"
    assert changes[1].additions == 3
    assert changes[1].deletions == 0

    assert changes[2].path == "image.png"
    assert changes[2].is_binary is True
    assert changes[2].additions == 0
    assert changes[2].deletions == 0
```

### GOOD: Compare whole objects (plain equality)

```python
def test_parse_numstat_output():
    numstat = "10\t5\tsrc/main.py\n3\t0\tREADME.md\n-\t-\timage.png"

    # Simple and obvious - just state what you expect
    assert parse_numstat_output(numstat) == [
        FileChange("src/main.py", additions=10, deletions=5, is_binary=False),
        FileChange("README.md", additions=3, deletions=0, is_binary=False),
        FileChange("image.png", additions=0, deletions=0, is_binary=True),
    ]
```

**Intent is clear**: Parse produces these exact changes. Simple, complete, obvious.

### BETTER: PyHamcrest for partial matching or composition

Use `has_properties()` when you need:

- **Partial matching** (only check some fields, ignore others)
- **Composed matchers** (e.g., `count=greater_than(0)`)

```python
from hamcrest import assert_that, has_properties, greater_than

def test_parse_creates_valid_change():
    result = parse_numstat_output("10\t5\tsrc/main.py")

    # Good: Check specific properties with matchers
    assert_that(result[0], has_properties(
        path="src/main.py",
        additions=greater_than(0),  # Composed matcher
        # Don't care about deletions, is_binary
    ))
```

**Benefits of PyHamcrest**:

- Better error message: "Expected: sequence containing [object with property 'path' equal to 'src/main.py', ...]"
- Order matters (use `contains_inanyorder` if order doesn't matter)
- Can match subset of properties
- Intent is clear: Parse produces these exact changes. Concise, complete.

**Rule**: When `has_properties()` lists ALL fields of a class with exact values, use plain `==` instead - it's simpler and clearer. **For full object comparison, prefer plain `==`**.

```python
# BAD: has_properties with all fields and exact values
assert_that(
    status,
    has_properties(
        status=Status.SKIPPED,
        note="Test skipped",
        value=None,
        timestamp=None,
        # ... all other fields
    ),
)

# GOOD: Full object equality
assert status == HabitStatus(
    status=Status.SKIPPED,
    note="Test skipped",
    value=None,
    timestamp=None,
    # ... all other fields
)
```

### Even Better: Parametrize multiple scenarios

```python
@pytest.mark.parametrize("numstat,expected", [
    ("10\t5\tfile.py", [FileChange("file.py", additions=10, deletions=5, is_binary=False)]),
    ("-\t-\timage.png", [FileChange("image.png", additions=0, deletions=0, is_binary=True)]),
    ("", []),
])
def test_parse_numstat_output(numstat, expected):
    assert parse_numstat_output(numstat) == expected
```

## Pattern 2: Verbose Collection Type Checks

### BAD: Multiple assertions about collection structure

```python
# BAD: Three separate assertions saying "non-empty list of Habit"
assert_that(habits, instance_of(list))
assert_that(habits, only_contains(instance_of(Habit)))
assert len(habits) > 0

# BAD: Two assertions about collection type
assert_that(areas, instance_of(list))
assert_that(areas, only_contains(instance_of(Area)))

# BAD: Checking specific type of collection when any collection works
assert_that(statuses, instance_of(list))
assert len(statuses) == 5
assert_that(statuses, only_contains(instance_of(HabitStatus)))
```

### GOOD: Concise composed assertions

```python
# GOOD: "non-empty collection of Habit" in one assertion
from hamcrest import assert_that, all_of, has_length, greater_than, only_contains, instance_of

assert_that(habits, all_of(
    has_length(greater_than(0)),
    only_contains(instance_of(Habit))
))

# GOOD: Just check content type, don't care if list/tuple
assert_that(areas, only_contains(instance_of(Area)))

# GOOD: "collection of exactly 5 HabitStatus"
assert_that(statuses, all_of(
    has_length(5),
    only_contains(instance_of(HabitStatus))
))
```

**Principle**: In tests, usually don't care if result is `list` vs `tuple` vs `set` - just care about the content. Don't assert `instance_of(list)` unless the specific collection type matters to the contract.

## Pattern 3: Collection Assertions

```python
# BAD: Manual assertions on collections
assert len(items) == 3
assert "foo" in items
assert items[0] == "first"

# GOOD: PyHamcrest matchers
from hamcrest import assert_that, has_length, has_item, contains_exactly

assert_that(items, has_length(3))
assert_that(items, has_item("foo"))
assert_that(items, contains_exactly("first", "second", "third"))
```

## Pattern 4: Type Checks

```python
# BAD: Manual isinstance
assert isinstance(result, User)
assert type(obj) == MyClass

# GOOD: PyHamcrest matchers (better error messages)
from hamcrest import assert_that, instance_of

assert_that(result, instance_of(User))
assert_that(obj, instance_of(MyClass))
```

## Pattern 5: String Assertions

```python
# BAD: Manual string checks
assert "error" in message
assert message.startswith("ERROR:")
assert re.match(r"^\d{3}-\d{4}$", code)

# GOOD: PyHamcrest matchers
from hamcrest import assert_that, contains_string, starts_with, matches_regexp

assert_that(message, contains_string("error"))
assert_that(message, starts_with("ERROR:"))
assert_that(code, matches_regexp(r"^\d{3}-\d{4}$"))
```

## Pattern 6: Complex Composite Assertions

```python
# BAD: Multiple separate assertions
assert result.status == "success"
assert result.count > 0
assert "error" not in result.message
assert isinstance(result.data, dict)

# GOOD: Plain equality if checking all fields with exact values
assert result == Result(
    status="success",
    count=5,
    message="All good",
    data={"key": "value"}
)

# BETTER: has_properties() for composed matchers or partial matching
from hamcrest import assert_that, has_properties, greater_than, not_, contains_string, instance_of

# When you need composition (>, <, contains, etc.)
assert_that(result, has_properties(
    status="success",
    count=greater_than(0),              # Not exact value - need matcher
    message=not_(contains_string("error")),  # Composition
    data=instance_of(dict)              # Type check, not exact value
))
```

**Rule**: Use plain `==` for exact full object comparison, `has_properties()` when you need matchers or partial matching.

## Pattern 7: Nested Object Matching (Real Codebase Examples)

PyHamcrest's true power shows when validating deeply nested structures. Here are patterns from the actual codebase that showcase composable matchers:

### Example 1: Nested has_properties for Complex Messages

**From: `test_persist_revive_continue.py:69-70`**

```python
# EXCELLENT: Check nested message structure in one assertion
assert_that(
    payloads,
    has_items(
        has_properties(
            type="ui_message",
            message=has_properties(mime="text/markdown", content="**hello**")
        ),
        has_finished_run(),
    ),
)
```

**Why this is better**:

- Single assertion validates entire nested structure
- `message=has_properties(...)` checks nested object properties
- `has_items()` validates multiple items in collection
- Clear intent: "Payloads contain a markdown message and finished status"

**BAD Alternative** (verbose, unclear intent):

```python
# Find ui_message
ui_msgs = [p for p in payloads if p.get("type") == "ui_message"]
assert len(ui_msgs) > 0
assert ui_msgs[0]["message"]["mime"] == "text/markdown"
assert ui_msgs[0]["message"]["content"] == "**hello**"

# Find finished run
run_statuses = [p for p in payloads if p.get("type") == "run_status"]
assert len(run_statuses) > 0
assert run_statuses[0]["run_state"]["status"] == RunStatus.FINISHED
```

### Example 2: Combining Matchers with all_of

**From: `helpers.py:151-153`**

```python
# Build matcher incrementally for optional conditions
m = has_properties(type="error", code=code_enum)
if message_substr:
    m = all_of(m, _message_contains(message_substr))  # Compose matchers!
assert_that(payloads, has_item(m))
```

**Why this is better**:

- `all_of()` combines multiple matchers into one
- Can build matchers conditionally
- Clear composition: "Match error AND message contains substring"

**BAD Alternative**:

```python
# Manual filtering and multiple assertions
errors = [p for p in payloads if p.get("type") == "error" and p.get("code") == code_enum]
assert len(errors) > 0
if message_substr:
    assert message_substr in errors[0]["message"]
```

### Example 3: Reusable Matcher Factories

**From: `ui_asserts.py:8-12`**

```python
def item_user_message(text: str | None = None):
    """Composable matcher for UserMessageItem with optional text check."""
    m = [instance_of(UserMessageItem)]
    if text is not None:
        m.append(has_properties(text=text))
    return all_of(*m)

# Usage:
assert_that(items, has_item(item_user_message(text="hello")))
```

**Why this pattern is excellent**:

- Reusable matcher function encapsulates common checks
- Combines type check (`instance_of`) with property validation
- `all_of(*m)` dynamically composes matchers based on parameters
- DRY: Single source of truth for "what makes a valid user message"

**BAD Alternative** (duplication across tests):

```python
# Test 1
assert isinstance(items[0], UserMessageItem)
assert items[0].text == "hello"

# Test 2
assert isinstance(items[1], UserMessageItem)
assert items[1].text == "world"

# Test 3 (forgot to check type!)
assert items[2].text == "foo"  # Bug: didn't verify it's UserMessageItem
```

### Example 4: Nested Validation in has_items

**From: `test_approval_ui_flow.py:44-47`**

```python
assert_that(
    payloads,
    has_items(
        has_properties(type="approval_pending", call_id="call_echo"),
        has_properties(type="approval_decision"),
        is_function_call_output_end_turn(call_id="call_ui_end"),
    ),
)
```

**Why this is better**:

- `has_items()` validates collection contains ALL matchers (order-independent)
- Mix of `has_properties()` and custom matchers (`is_function_call_output_end_turn()`)
- Clear intent: "These three events must be present"

**vs. contains_exactly()** (when order matters):

```python
# Use contains_exactly() when order is significant
assert_that(
    payloads,
    contains_exactly(
        has_properties(type="start"),
        has_properties(type="processing"),
        has_properties(type="complete"),
    )
)
```

### Example 5: Combining instance_of with has_properties

**From: Scan findings - pattern to adopt**

```python
# BAD: Separate type and property checks
assert isinstance(result, ToolCallExecution)
assert result.exit_code == 0
assert result.stdout.startswith("Success")

# GOOD: Composed matcher
from hamcrest import assert_that, all_of, instance_of, has_properties, starts_with

assert_that(result, all_of(
    instance_of(ToolCallExecution),
    has_properties(
        exit_code=0,
        stdout=starts_with("Success")
    )
))
```

**Why composition matters**:

- Single assertion point of failure
- Better error messages: "Expected: instance of ToolCallExecution AND properties..."
- Clear test intent in one expression

## Pattern 8: Custom Matcher Helpers

The codebase has excellent examples of reusable matcher factories:

```python
# From ws_helpers.py:317
def has_finished_run():
    """Matcher: run_status with status == finished."""
    return has_properties(
        type="run_status",
        run_state=has_properties(status=RunStatus.FINISHED)
    )

# From helpers.py:139
def _message_contains(fragment: str):
    """Matcher ensuring an error message contains fragment."""
    return has_properties(message=contains_string(fragment))

# Usage becomes ultra-readable:
assert_that(payloads, has_item(has_finished_run()))
assert_that(errors, has_item(_message_contains("timeout")))
```

**Benefits of matcher factories**:

1. **DRY**: Define complex match logic once
2. **Readable**: `has_finished_run()` reads like English
3. **Composable**: Can combine with `all_of()`, `any_of()`
4. **Maintainable**: Change run status structure? Update matcher in one place
5. **Type-safe**: IDE autocomplete for custom matchers

## Key Composability Patterns

### 1. all_of() - AND logic

```python
assert_that(user, all_of(
    instance_of(User),
    has_properties(active=True, verified=True)
))
# "Must be User AND active AND verified"
```

### 2. any_of() - OR logic

```python
assert_that(status, any_of(
    equal_to(Status.SUCCESS),
    equal_to(Status.PENDING)
))
# "Must be SUCCESS OR PENDING"
```

### 3. `not_()` - Negation

```python
assert_that(message, not_(contains_string("error")))
# "Must NOT contain 'error'"
```

### 4. Nested matchers

```python
assert_that(response, has_properties(
    status=200,
    body=has_properties(
        data=has_items(
            has_properties(id=1),
            has_properties(id=2)
        )
    )
))
# "Response with status 200, body.data contains items with id 1 and 2"
```

## When to Extract Matcher Factories

Extract when a pattern appears **3+ times** OR is **conceptually significant**:

```python
# Pattern appears 5+ times in tests:
# assert p.get("type") == "approval_pending" and p.get("call_id") == call_id

# Extract to:
def has_approval_pending(call_id: str):
    return has_properties(type="approval_pending", call_id=call_id)

# Now tests read clearly:
assert_that(payloads, has_item(has_approval_pending("call_echo")))
```

## Detection Strategy

**MANDATORY Step 0**: Discover ALL assert statements in test files.

- This scan is **required** - do not skip this step
- You **must** read and process ALL assert output using your intelligence
- High recall required, high precision NOT required - you determine which would benefit from PyHamcrest
- Review each assertion for: verbose patterns, field-by-field checks, collection operations, type checks
- Prevents lazy analysis by forcing examination of ALL test assertions

```bash
# Find ALL assert statements in test files with context
rg --type py '^[[:space:]]*assert ' --glob "test_*.py" --glob "*_test.py" -B 1 -A 1 --line-number

# Count total assertions found
rg --type py '^[[:space:]]*assert ' --glob "test_*.py" --glob "*_test.py" | wc -l
```

**What to review for each assertion:**

1. **Field-by-field checks**: Multiple `assert obj.field ==` for same object (use plain `==` or `has_properties()`)
2. **Collection operations**: `assert len()`, `assert x in`, `assert x[0] ==` (use `has_length()`, `has_item()`, matchers)
3. **Type checks**: `assert isinstance()` (use `instance_of()` for better error messages)
4. **String operations**: `assert "x" in str`, `assert str.startswith()` (use `contains_string()`, `starts_with()`)
5. **Numeric comparisons**: `assert x > y`, `assert x >= y` (use `greater_than()`, `greater_than_or_equal_to()`)
6. **Verbose collection type checks**: Multiple assertions about same collection (compose with `all_of()`)

**Process ALL output**: Read each assertion, use your judgment to identify which would benefit from PyHamcrest matchers.

**Key decision**: For full object comparison with exact values, prefer plain `==`. Use `has_properties()` only for partial matching or composed matchers.

---

**Primary**: Manual code reading - read test files thoroughly, look for verbose patterns.

**Automated preprocessing AFTER Step 0** (targeted patterns, manual verification required):

```bash
# Verbose collection checks (3+ lines about same collection)
rg --type py "assert_that.*instance_of\(list\)" --glob "test_*.py" -A2 | grep -E "(only_contains|len)"

# Field-by-field patterns
rg --type py "assert \w+\.\w+ ==" --glob "test_*.py" -A1 | grep "assert"

# Collection operations that have matchers
rg --type py "assert len\(" --glob "test_*.py"
rg --type py "assert .* in " --glob "test_*.py"

# Type checks
rg --type py "assert isinstance\(" --glob "test_*.py"

# String operations
rg --type py 'assert ".*" in \w+' --glob "test_*.py"
rg --type py "assert \w+\.startswith\(" --glob "test_*.py"

# Numeric comparisons
rg --type py "assert \w+ [><]=" --glob "test_*.py"

# has_properties with many fields (might be better as full ==)
rg --type py "has_properties\(" --glob "test_*.py" -A10
```

**Important**: Grep patterns find candidates for manual review. Don't trust them blindly. Read the actual code to understand context and determine if changes make sense.

## References

- [PyHamcrest Documentation](https://pyhamcrest.readthedocs.io/)
- [Effective Python Testing](https://realpython.com/pytest-python-testing/)
- [Test Clarity](https://www.satisfice.com/blog/archives/856)
