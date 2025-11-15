# Scan: Test Assertion Antipatterns

## Context
@../shared-context.md

## Pattern: Field-by-Field Assertions Instead of Object Comparison

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

### GOOD: Compare whole objects

```python
def test_parse_numstat_output():
    numstat = "10\t5\tsrc/main.py\n3\t0\tREADME.md\n-\t-\timage.png"

    assert parse_numstat_output(numstat) == [
        FileChange("src/main.py", additions=10, deletions=5, is_binary=False),
        FileChange("README.md", additions=3, deletions=0, is_binary=False),
        FileChange("image.png", additions=0, deletions=0, is_binary=True),
    ]
```

**Intent is clear**: Parse produces these exact changes. Concise, complete.

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

## Issues with Field-by-Field

- **Verbose** - 20 lines → 5 lines
- **Fragile** - Add a field? Update every test
- **Incomplete** - Easy to forget fields
- **Unclear intent** - What's the expected object?
- **Harder to read** - Scattered assertions vs clear data structure

## Detection

```bash
# Find tests with many assertions on same object
rg --type py -A20 "def test_" | rg "assert.*\[0\].*==" | head -20

# Find patterns like: assert obj.field1 == x; assert obj.field2 == y
rg --type py "assert \w+\.\w+ ==" --glob "test_*.py" -A1 | grep "assert"
```

## Fix Strategy

1. **Use `==` on whole objects** (if dataclass/Pydantic with `__eq__`)
2. **Use pytest.approx for floats** when needed
3. **Use structured comparison** (dict, list, tuple)
4. **Parametrize** for multiple test cases

## When Field-by-Field Is Okay

- **Object has 50+ fields** and you only care about 3
- **Testing specific field transformations** in isolation
- **Partial matching** with complex nested structures

But even then, consider extracting relevant fields into a test helper:

```python
def test_api_response_includes_user():
    resp = get_user_details(123)
    # Extract what matters
    user = (resp.user.id, resp.user.name, resp.user.email)
    assert user == (123, "Alice", "alice@example.com")
```

## References

- [Effective Python Testing](https://realpython.com/pytest-python-testing/)
- [Test Clarity](https://www.satisfice.com/blog/archives/856)
