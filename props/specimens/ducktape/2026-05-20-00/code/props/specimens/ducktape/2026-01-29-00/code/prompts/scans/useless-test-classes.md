# Scan: Useless Test Classes

Identify pytest test classes that don't provide value and should be converted to module-level test functions.

## Antipattern

Test classes that:

1. Don't define any class-level fixtures (via `@pytest.fixture` decorators in the class)
2. Don't have setup/teardown methods (`setup_method`, `teardown_method`, `setup_class`, `teardown_class`)
3. Don't maintain shared state between tests
4. Only use module-level fixtures from conftest.py
5. Are just containers for grouping related tests

## Why It's Bad

- Adds unnecessary indentation and boilerplate
- Misleads readers into thinking there's shared state or setup logic
- Pytest can organize tests just fine with module-level functions using naming conventions
- Class-based tests have a performance cost (minimal, but exists)
- Violates YAGNI (You Aren't Gonna Need It) principle

## Good Pattern

**ONLY use test classes when they provide value:**

1. **Class-level fixtures** - Shared expensive setup:

```python
class TestDatabase:
    @pytest.fixture(scope="class")
    def db_connection(self):
        conn = create_expensive_connection()
        yield conn
        conn.close()

    def test_query(self, db_connection):
        # Uses class-level fixture
        ...
```

2. **Setup/teardown methods** - State management:

```python
class TestStatefulComponent:
    def setup_method(self):
        self.state = ComponentState()

    def teardown_method(self):
        self.state.cleanup()

    def test_operation(self):
        self.state.do_something()
        assert self.state.value == expected
```

3. **Shared instance attributes** - Mutable state across tests:

```python
class TestCounter:
    def setup_class(self):
        self.counter = 0

    def test_increment(self):
        self.counter += 1
        assert self.counter == 1
```

## Bad Pattern (Convert to Module-Level Functions)

```python
# BAD - Class provides no value
class TestHabitifyClient:
    """Tests for the Habitify client using async methods only."""

    async def test_get_habits(self, client, mock_async_response, patch_client_method):
        # All fixtures are from conftest.py
        # No shared state, no setup methods, no class-level fixtures
        mock_resp = mock_async_response("get_habits.yaml")
        with patch_client_method("get", return_value=mock_resp) as mock_get:
            habits = await client.get_habits()
            assert habits[0].id == "-Lo9NTLRX3aCxg-PjN25"

    async def test_get_habit(self, client, mock_async_response, patch_client_method):
        # Same pattern - just using module-level fixtures
        ...
```

**GOOD - Convert to module-level functions:**

```python
# GOOD - Module-level functions are cleaner
async def test_get_habits(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habits.yaml")
    with patch_client_method("get", return_value=mock_resp) as mock_get:
        habits = await client.get_habits()
        assert habits[0].id == "-Lo9NTLRX3aCxg-PjN25"

async def test_get_habit(client, mock_async_response, patch_client_method):
    ...
```

## Detection Strategy

For each test class:

1. Check if it has any setup/teardown methods:

```bash
rg --type py "class Test\w+:" -A 100 | grep -E "(setup_method|teardown_method|setup_class|teardown_class)"
```

2. Check if it defines class-level fixtures:

```bash
rg --type py "class Test\w+:" -A 100 | grep -E "@pytest\.fixture.*scope.*class"
```

3. Check if test methods use `self.` for shared state:

```bash
rg --type py "def test_\w+\(self" -A 20 | grep "self\.\w+ ="
```

4. If none of the above, the class is likely useless and should be converted to module-level functions.

## Manual Review Process

For each `class Test*:` found:

1. **Check for class-level pytest fixtures** (decorated with `@pytest.fixture`)
   - If found: Class provides value, KEEP IT

2. **Check for setup/teardown methods**
   - `setup_method`, `teardown_method`, `setup_class`, `teardown_class`
   - If found: Class provides value, KEEP IT

3. **Check for shared state via `self.`**
   - Look for `self.attribute = value` patterns
   - If found and used across multiple tests: Class provides value, KEEP IT

4. **If none of the above:**
   - Class is just a container
   - Convert to module-level functions
   - Remove class wrapper and dedent all methods

## Example Conversion

**Before:**

```python
class TestHabitifyClient:
    """Tests for the Habitify client using async methods only."""

    async def test_get_habits(self, client, mock_async_response, patch_client_method):
        mock_resp = mock_async_response("get_habits.yaml")
        with patch_client_method("get", return_value=mock_resp) as mock_get:
            habits = await client.get_habits()
            mock_get.assert_called_once_with("/habits")
            assert habits[0].id == "-Lo9NTLRX3aCxg-PjN25"

    async def test_get_habit(self, client, mock_async_response, patch_client_method):
        mock_resp = mock_async_response("get_habit_by_id.yaml")
        with patch_client_method("get", return_value=mock_resp) as mock_get:
            habit = await client.get_habit("-Lo9NTLRX3aCxg-PjN25")
            assert habit.id == "-Lo9NTLRX3aCxg-PjN25"
```

**After:**

```python
"""Tests for the Habitify client using async methods only."""

async def test_habitify_client_get_habits(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habits.yaml")
    with patch_client_method("get", return_value=mock_resp) as mock_get:
        habits = await client.get_habits()
        mock_get.assert_called_once_with("/habits")
        assert habits[0].id == "-Lo9NTLRX3aCxg-PjN25"

async def test_habitify_client_get_habit(client, mock_async_response, patch_client_method):
    mock_resp = mock_async_response("get_habit_by_id.yaml")
    with patch_client_method("get", return_value=mock_resp) as mock_get:
        habit = await client.get_habit("-Lo9NTLRX3aCxg-PjN25")
        assert habit.id == "-Lo9NTLRX3aCxg-PjN25"
```

**Changes:**

- Removed `class TestHabitifyClient:`
- Moved class docstring to module docstring
- Dedented all test methods by one level
- Renamed tests to include context prefix (e.g., `test_get_habits` → `test_habitify_client_get_habits`)
- Tests still use the same fixtures from conftest.py

## Files to Review

Common locations:

- `*/tests/test_*.py`
- `*/tests/**/test_*.py`

Priority files (based on search):

- `llm/mcp/habitify/habitify_mcp_server/tests/test_habitify_client.py`
- `claude/claude_hooks/tests/test_autofixer.py`
- `claude/claude_hooks/tests/test_protocol_actions.py`
- `wt/tests/*/test_*.py` (multiple files)
- `llm/ducktape_llm_common/tests/*/test_*.py` (multiple files)

## False Positives (Classes That ARE Valuable)

These patterns indicate the class SHOULD be kept:

1. **Parameterized test classes** - Using `pytest.mark.parametrize` at class level
2. **Test organization with pytest.mark** - Using class-level markers (e.g., `@pytest.mark.slow`)
3. **Inheritance hierarchies** - Base test classes with shared behavior
4. **Plugin integration** - Classes required by pytest plugins

**Example of valuable class:**

```python
@pytest.mark.slow
@pytest.mark.integration
class TestDatabaseIntegration:
    # Class-level markers apply to all methods - this is valuable!
    def test_query(self):
        ...
    def test_insert(self):
        ...
```
