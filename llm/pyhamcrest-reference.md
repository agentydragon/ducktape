# PyHamcrest Reference Guide

PyHamcrest is a framework for writing matcher objects, allowing you to declaratively define "match" rules. This guide covers common usage patterns and examples.

## Installation

```bash
pip install PyHamcrest
```

## Basic Usage

```python
from hamcrest import *
```

## Common Matchers

### Equality and Identity

```python
# ❌ Don't use matchers for simple equality
assert_that(42, equal_to(42))  # Overcomplicated!

# ✅ Use simple == for exact equality
assert 42 == 42
assert "hello" == "hello"

# Use matchers only for special cases:
# Same instance check
obj = MyClass()
assert_that(obj, is_(same_instance(obj)))

# When you need better error messages
assert_that(complex_object, equal_to(expected_object))  # Shows detailed diff
```

### None Checks

```python
# Is None
assert_that(None, is_(none()))

# Is not None
assert_that("value", is_not(none()))
assert_that({}, not_none())
```

### Boolean Matchers

```python
# True/False checks
assert_that(True, is_(True))
assert_that(False, is_(False))

# Truthy/Falsy
assert_that([], is_(empty()))
assert_that([1, 2, 3], is_not(empty()))
```

### Number Comparisons

```python
# ❌ Don't use matchers for simple comparisons
assert_that(10, greater_than(5))  # Overcomplicated!
assert_that(5, less_than(10))  # Overcomplicated!

# ✅ Use simple comparisons
assert 10 > 5
assert 5 < 10
assert x >= 10
assert y <= 5

# ✅ Use matchers when nested in other matchers
assert_that(response, has_entries({
    "count": greater_than(0),
    "score": less_than(100)
}))

# ✅ Use matchers for special numeric checks
assert_that(3.14159, close_to(3.14, 0.01))  # Approximate equality

# Simple type checks - use isinstance
assert isinstance(42, int)  # Not assert_that(42, instance_of(int))
```

### String Matchers

```python
# Contains substring
assert_that("hello world", contains_string("world"))

# Starts/Ends with
assert_that("hello world", starts_with("hello"))
assert_that("hello world", ends_with("world"))

# Case insensitive
assert_that("Hello", equal_to_ignoring_case("hello"))

# Whitespace normalization
assert_that("hello   world", equal_to_ignoring_whitespace("hello world"))

# Matches regex
assert_that("abc123", matches_regexp(r"^[a-z]+\d+$"))

# String length
assert_that("hello", has_length(5))
```

### Collection Matchers

```python
# Has item
assert_that([1, 2, 3], has_item(2))

# Has items (all must be present)
assert_that([1, 2, 3, 4], has_items(1, 3))

# Contains exactly (order matters)
assert_that([1, 2, 3], contains_exactly(1, 2, 3))

# Contains in any order
assert_that([3, 1, 2], contains_inanyorder(1, 2, 3))

# Has length
assert_that([1, 2, 3], has_length(3))

# Empty
assert_that([], is_(empty()))
assert_that([1], is_not(empty()))

# Contains specific matchers
assert_that([1, 2, 3], has_item(greater_than(2)))

# All items match
assert_that([2, 4, 6], only_contains(even()))
assert_that(["a", "b", "c"], evenly_matches(instance_of(str)))
```

### Dictionary Matchers

```python
# Has key
assert_that({"name": "John"}, has_key("name"))

# Has value
assert_that({"name": "John"}, has_value("John"))

# ❌ Don't use has_entry for exact match
assert_that({"name": "John"}, has_entry("name", "John"))  # Overcomplicated!

# ✅ Use simple equality
assert data["name"] == "John"

# ✅ Prefer kwargs for has_entries
assert_that(
    {"name": "John", "age": 30},
    has_entries(name="John", age=30)  # Good - using kwargs
)

# ❌ Avoid dict argument when kwargs work
assert_that(
    {"a": 1, "b": 2},
    has_entries({"a": 1, "b": 2})  # Less readable
)

# ✅ Use kwargs when possible
assert_that(
    {"a": 1, "b": 2, "c": 3},
    has_entries(a=1, b=2)  # Cleaner with kwargs
)

# ✅ Use dict when keys aren't valid Python identifiers
assert_that(
    {"foo/bar": 1, "baz-qux": 2},
    has_entries({"foo/bar": 1, "baz-qux": 2})  # Must use dict
)

# ✅ Use matchers for flexible checks
assert_that(
    {"name": "John", "age": 30},
    has_entries(
        name=starts_with("J"),
        age=greater_than(25)
    )
)
```

### Object and Type Matchers

```python
# ❌ Don't use matchers for simple type checks
assert_that("hello", instance_of(str))  # Overcomplicated!

# ✅ Use isinstance for simple checks
assert isinstance("hello", str)
assert isinstance([1, 2, 3], list)

# ✅ Use matchers for property checks and complex assertions
class Person:
    def __init__(self, name, age):
        self.name = name
        self.age = age

person = Person("John", 30)

# Property existence
assert_that(person, has_property("name"))

# ❌ Don't use matcher for exact property value
assert_that(person, has_property("name", "John"))  # Overcomplicated!

# ✅ Use simple attribute access
assert person.name == "John"

# ✅ Use matchers when property needs flexible matching
assert_that(person, has_property("age", greater_than(25)))

# ✅ Use matchers for multiple properties with mixed checks
assert_that(
    person,
    has_properties(
        name="John",
        age=greater_than(18)  # Flexible check
    )
)
```

### Combining Matchers

```python
# All of (AND)
assert_that(5, all_of(
    greater_than(0),
    less_than(10),
    instance_of(int)
))

# Any of (OR)
assert_that("hello", any_of(
    equal_to("hello"),
    equal_to("world")
))

# Not
assert_that(5, is_not(equal_to(10)))
assert_that("hello", is_not(none()))

# Is (readability helper) - but prefer simple assertions
assert 5 == 5  # Better than assert_that(5, is_(equal_to(5)))
assert flag is True  # Better than assert_that(flag, is_(True))
```

### Exception Matchers

```python
# Raises exception
assert_that(calling(lambda: 1/0), raises(ZeroDivisionError))

# Raises with message
assert_that(calling(lambda: raise_error()), raises(ValueError, "Invalid"))

# Raises with message pattern
assert_that(
    calling(lambda: raise_error()),
    raises(ValueError, matching(contains_string("invalid")))
)

# With context manager
with assert_raises(ValueError):
    raise ValueError("Test error")
```

## Custom Matchers

```python
# Basic custom matcher
class IsEven(BaseMatcher):
    def _matches(self, item):
        return item % 2 == 0
    
    def describe_to(self, description):
        description.append_text("an even number")
    
    def describe_mismatch(self, item, mismatch_description):
        mismatch_description.append_text(f"{item} is odd")

def even():
    return IsEven()

# Usage
assert_that(4, is_(even()))
assert_that(5, is_not(even()))

# Custom matcher with parameter
class DivisibleBy(BaseMatcher):
    def __init__(self, divisor):
        self.divisor = divisor
    
    def _matches(self, item):
        return item % self.divisor == 0
    
    def describe_to(self, description):
        description.append_text(f"divisible by {self.divisor}")

def divisible_by(n):
    return DivisibleBy(n)

# Usage
assert_that(15, is_(divisible_by(3)))
assert_that(15, is_(divisible_by(5)))
```

## Common Patterns

### Testing with Matchers

```python
# In pytest
def test_user_creation():
    user = create_user("John", 30)
    
    assert_that(user, has_properties(
        name="John",
        age=30,
        id=instance_of(int),
        created_at=instance_of(datetime)
    ))

# Multiple assertions
def test_api_response():
    response = api_call()
    
    assert_that(response, all_of(
        has_property("status", equal_to(200)),
        has_property("data", has_key("users")),
        has_property("data", has_entry("users", has_length(greater_than(0))))
    ))
```

### Flexible Matching

```python
# Match any of several values
assert_that(status_code, is_in([200, 201, 204]))

# Match range
assert_that(score, all_of(
    greater_than_or_equal_to(0),
    less_than_or_equal_to(100)
))

# Partial dictionary matching
expected_subset = {"status": "success", "code": 200}
assert_that(response, has_entries(expected_subset))

# List containing specific items in any position
assert_that(items, has_items("apple", "banana"))
```

### Descriptive Assertions

```python
# Custom descriptions for better error messages
assert_that(
    user.age,
    greater_than(18),
    reason="User must be an adult"
)

# Combining for clarity
assert_that(
    password,
    all_of(
        has_length(greater_than(8)),
        matches_regexp(r".*[A-Z].*"),
        matches_regexp(r".*[0-9].*")
    ),
    reason="Password must be 8+ chars with uppercase and number"
)
```

## Tips and Best Practices

1. **Prefer simple assertions**: Use `assert x == 5` not `assert_that(x, equal_to(5))`
2. **Import style**: Use `from hamcrest import *` for readability
3. **Use matchers for flexibility**: Not for simple equality checks
4. **Combine matchers**: Use `all_of` and `any_of` for complex assertions
5. **Custom matchers**: Create them for domain-specific assertions
6. **Descriptive failures**: Add `reason` parameter for context
7. **Match collections flexibly**: Use `has_items` instead of exact matching when order doesn't matter

## When NOT to Use PyHamcrest

### Use Simple == When You Know the Exact Expected Value

PyHamcrest is for flexible matching. When you know the exact expected value, use `==`:

```python
# ❌ Overcomplicated - Don't use matchers for exact equality
assert_that(controller_deps, has_entries({
    "models/user.py": contains("userModel"),
    "validators/user.py": contains("userValidator"),
    "utils/logger.py": contains("logger")
}))

# ✅ Simple and clear - Use == when you know exact structure
assert controller_deps == {
    "models/user.py": {"userModel"},
    "validators/user.py": {"userValidator"},
    "utils/logger.py": {"logger"}
}

# ❌ Unnecessary for exact match
assert_that(result, equal_to({"status": "ok", "code": 200}))

# ✅ Direct comparison is clearer
assert result == {"status": "ok", "code": 200}
```

### When You MUST Use PyHamcrest

Use matchers when the exact value is unknown or flexible:

```python
# ✅ Good use - when set might have additional unknown items
assert_that(deps["models/user.py"], contains("userModel"))
# This passes even if the set is {"userModel", "BaseModel", "utils"}

# ✅ Good use - checking subset of dictionary
assert_that(response, has_entries({"status": "ok"}))
# This passes even if response has other keys like "timestamp", "id", etc.

# ✅ Good use - flexible list matching
assert_that(log_entries, has_item(contains_string("ERROR")))
# This passes if ANY item contains "ERROR"
```

### Guidelines

**Use `==` when:**
- You know the complete, exact expected value
- You want the test to fail if ANYTHING differs
- The structure is simple and fixed

**Use PyHamcrest when:**
- The value might have unknown additional elements
- You only care about specific parts
- You need flexible matching (contains, greater than, etc.)
- The exact value varies but follows patterns

```python
# Example: Testing a function that returns a set of dependencies
def test_exact_dependencies():
    # When you control inputs and know exact output
    deps = analyze_file("simple.py")
    assert deps == {"os", "sys"}  # Must be EXACTLY these two

def test_minimum_dependencies():
    # When file might import more than you care about
    deps = analyze_file("complex.py")
    assert_that(deps, has_items("os", "sys"))  # At least these two
```

## Common Pitfalls

```python
# ❌ Don't use matchers for simple equality
assert_that(x, equal_to(5))  # Overcomplicated!

# ✅ Use simple assertions
assert x == 5

# ❌ Don't pass boolean expressions to assert_that
assert_that(value == expected)  # Wrong - this passes True/False to assert_that!

# ✅ Use simple assert or proper matcher
assert value == expected  # For exact equality
assert_that(value, close_to(expected, 0.1))  # For flexibility

# ❌ Don't nest assert_that
assert_that(assert_that(x, equal_to(5)))  # Wrong!

# ✅ Use combining matchers if needed
assert_that(x, all_of(greater_than(0), less_than(10)))

# ❌ Don't write redundant existence checks
assert 'processOrder' in tracker.nodes  # Redundant!
assert 'validateOrder' in tracker.nodes  # Redundant!
assert tracker.nodes['processOrder'].type == 'function'  # This already checks existence
assert tracker.nodes['validateOrder'].type == 'function'

# ✅ Just do the real check
assert tracker.nodes['processOrder'].type == 'function'
assert tracker.nodes['validateOrder'].type == 'function'
# If the key doesn't exist, you'll get a clear KeyError

# ❌ Don't check keys exist one by one
data = json.loads(result.stdout)
assert 'ast' in data         # Redundant!
assert 'nodeInfo' in data    # Redundant!
assert 'stats' in data       # Redundant!
# ... then later use data['ast'], data['nodeInfo'], etc.

# ✅ Just use the data - let it fail clearly if keys missing
data = json.loads(result.stdout)
ast_data = data['ast']  # Clear KeyError if missing
node_info = data['nodeInfo']
stats = data['stats']

# ✅ Or check all required keys at once
expected_keys = {'ast', 'nodeInfo', 'prettyPrints', 'source', 'stats'}
assert data.keys() >= expected_keys  # Has at least these keys

# ✅ Or if you want to verify structure, check something meaningful
assert data['stats']['lineCount'] > 0
assert len(data['ast']['nodes']) > 0
```

## Integration with Test Frameworks

### pytest
```python
import pytest
from hamcrest import *

def test_something():
    assert_that(calculate(), equal_to(42))

# Works with pytest.raises
with pytest.raises(ValueError) as exc_info:
    risky_operation()
assert_that(str(exc_info.value), contains_string("invalid"))
```

### unittest
```python
import unittest
from hamcrest import *

class TestSomething(unittest.TestCase):
    def test_example(self):
        assert_that(2 + 2, equal_to(4))
```

## Quick Reference Table

| Matcher | Example | Description |
|---------|---------|-------------|
| `equal_to(x)` | `assert_that(5, equal_to(5))` | Exact equality |
| `close_to(x, delta)` | `assert_that(3.14, close_to(3.1, 0.1))` | Numeric proximity |
| `contains_string(s)` | `assert_that("hello", contains_string("ell"))` | Substring check |
| `has_length(n)` | `assert_that([1,2,3], has_length(3))` | Collection/string length |
| `has_item(x)` | `assert_that([1,2,3], has_item(2))` | Collection contains item |
| `has_entry(k, v)` | `assert_that({"a": 1}, has_entry("a", 1))` | Dict has key-value |
| `instance_of(type)` | `assert_that("hi", instance_of(str))` | Type check |
| `none()` | `assert_that(None, is_(none()))` | None check |
| `empty()` | `assert_that([], is_(empty()))` | Empty collection |
| `all_of(*matchers)` | `assert_that(5, all_of(greater_than(0), less_than(10)))` | All conditions |
| `any_of(*matchers)` | `assert_that(5, any_of(equal_to(5), equal_to(10)))` | Any condition |
