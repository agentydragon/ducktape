# Scan: Duplicated Code Patterns

## Context

@../shared-context.md

## Overview

Codebases accumulate duplicated code across all domains:

- **Test code**: Identical fixtures, setup logic, assertion patterns, custom matchers
- **Production code**: Repeated business logic, data transformations, validation patterns
- **Utilities**: Similar helper functions with slight variations
- **Configuration**: Duplicated setup/teardown patterns

This scan identifies opportunities to extract shared implementations to reduce maintenance burden and improve consistency.

## Core Principle

**DRY (Don't Repeat Yourself) matters**: While local clarity sometimes justifies duplication, systematic patterns should be factored into shared implementations.

**Balance**: Prefer local clarity over premature abstraction, but extract when:

- Pattern appears 3+ times across different modules
- Logic is complex and error-prone to duplicate
- Pattern needs to evolve consistently
- Changes require updating multiple locations

## Test Code Duplication (Primary Focus)

Test code is particularly prone to duplication. While test clarity sometimes justifies local duplication, systematic patterns should be factored into shared fixtures, conftest.py helpers, or custom matchers.

## Pattern 1: Duplicated Fixtures

### BAD: Same fixture in multiple test files

```python
# tests/test_user_api.py
@pytest.fixture
def test_user():
    return User(id=1, name="Test User", email="test@example.com")

# tests/test_user_service.py
@pytest.fixture
def test_user():
    return User(id=1, name="Test User", email="test@example.com")

# tests/test_user_repo.py
@pytest.fixture
def test_user():
    return User(id=1, name="Test User", email="test@example.com")
```

### GOOD: Shared fixture in conftest.py

```python
# tests/conftest.py
@pytest.fixture
def test_user():
    """Standard test user for all test modules."""
    return User(id=1, name="Test User", email="test@example.com")

# tests/test_user_api.py
def test_get_user(test_user, client):  # Fixture injected
    response = client.get(f"/users/{test_user.id}")
    assert response.json()["name"] == test_user.name
```

## Pattern 2: Duplicated Setup Logic

### BAD: Repeated database/file setup

```python
# tests/test_db_queries.py
async def test_query_users():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        # ... test logic

# tests/test_db_mutations.py
async def test_create_user():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        # ... test logic
```

### GOOD: Fixture-based setup

```python
# tests/conftest.py
@pytest.fixture
async def db_session():
    """Provide isolated database session for tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine) as session:
        yield session
    await engine.dispose()

# tests/test_db_queries.py
async def test_query_users(db_session):
    users = await db_session.execute(select(User))
    # ... test logic
```

## Pattern 3: Duplicated Custom Matchers

### BAD: Repeated assertion helpers

```python
# tests/test_api_responses.py
def assert_valid_user_response(data):
    assert "id" in data
    assert "name" in data
    assert "email" in data
    assert isinstance(data["id"], int)

# tests/test_user_serialization.py
def assert_valid_user_response(data):
    assert "id" in data
    assert "name" in data
    assert "email" in data
    assert isinstance(data["id"], int)
```

### GOOD: Shared matcher

```python
# tests/matchers.py (or conftest.py)
from hamcrest import has_entries, instance_of

def valid_user_response():
    """Matcher for valid user response dictionaries."""
    return has_entries(
        id=instance_of(int),
        name=instance_of(str),
        email=instance_of(str),
    )

# tests/test_api_responses.py
from tests.matchers import valid_user_response

def test_get_user_returns_valid_response(client):
    response = client.get("/users/1")
    assert_that(response.json(), valid_user_response())
```

## Pattern 4: Duplicated Mock Configurations

### BAD: Same mock setup across tests

```python
# tests/test_email_service.py
@patch("app.services.email.EmailClient")
def test_send_welcome_email(mock_client):
    mock_client.return_value.send.return_value = {"status": "sent"}
    # ... test

# tests/test_notification_service.py
@patch("app.services.email.EmailClient")
def test_send_notification(mock_client):
    mock_client.return_value.send.return_value = {"status": "sent"}
    # ... test
```

### GOOD: Mock fixture

```python
# tests/conftest.py
@pytest.fixture
def mock_email_client():
    """Provide configured mock email client."""
    with patch("app.services.email.EmailClient") as mock:
        mock.return_value.send.return_value = {"status": "sent"}
        yield mock.return_value

# tests/test_email_service.py
def test_send_welcome_email(mock_email_client):
    send_welcome_email("user@example.com")
    mock_email_client.send.assert_called_once()
```

### CRITICAL: Don't Mock Trivial Data Holders

**ANTI-PATTERN**: Mocking Pydantic models, dataclasses, or simple data holders

```python
# BAD: Mock reimplements the data structure
def make_mock_message(name: str, arguments: dict[str, Any] | None = None):
    """Create a mock MCP CallToolRequest message."""
    class MockMessage:
        def __init__(self, name: str, arguments: dict[str, Any] | None):
            self.name = name
            self.arguments = arguments or {}
    return MockMessage(name, arguments)

# BAD: Using unittest.mock for simple data
from unittest.mock import Mock
def test_process_request():
    mock_request = Mock()
    mock_request.name = "tool_name"
    mock_request.arguments = {"key": "value"}
    process(mock_request)
```

**GOOD**: Use real instances with test data

```python
# GOOD: Use the actual Pydantic model
from mcp.types import CallToolRequest

def test_process_request():
    request = CallToolRequest(
        name="tool_name",
        arguments={"key": "value"}
    )
    process(request)

# GOOD: Use actual dataclass
from dataclasses import dataclass

@dataclass
class User:
    id: int
    name: str

def test_user_validation():
    user = User(id=1, name="Test User")  # Real instance
    assert validate_user(user)
```

**Why?**

- Mocks hide schema changes (real models fail fast)
- Mocks don't validate constraints (Pydantic validation, required fields)
- Mocks create maintenance burden (reimplementing data structures)
- Real instances are self-documenting (IDE autocomplete, type hints)
- Tests become integration-like (closer to production behavior)

**When to mock**: Only mock classes with behavior (services, clients, I/O)
**When NOT to mock**: Never mock pure data containers (Pydantic, dataclasses, NamedTuple, TypedDict)

## Pattern 5: Duplicated Parameterization

### BAD: Repeated test data across modules

```python
# tests/test_validation.py
@pytest.mark.parametrize("invalid_email", [
    "notanemail",
    "@missinglocal.com",
    "missing@domain",
    "spaces in@email.com",
])
def test_email_validation_rejects_invalid(invalid_email):
    assert not is_valid_email(invalid_email)

# tests/test_user_creation.py
@pytest.mark.parametrize("invalid_email", [
    "notanemail",
    "@missinglocal.com",
    "missing@domain",
    "spaces in@email.com",
])
def test_create_user_rejects_invalid_email(invalid_email):
    with pytest.raises(ValidationError):
        create_user(email=invalid_email)
```

### GOOD: Shared test data

```python
# tests/test_data.py
INVALID_EMAILS = [
    "notanemail",
    "@missinglocal.com",
    "missing@domain",
    "spaces in@email.com",
]

# tests/test_validation.py
from tests.test_data import INVALID_EMAILS

@pytest.mark.parametrize("invalid_email", INVALID_EMAILS)
def test_email_validation_rejects_invalid(invalid_email):
    assert not is_valid_email(invalid_email)
```

## Pattern 6: Duplicated Assertion Patterns

### BAD: Repeated complex assertions

```python
# tests/test_user_endpoints.py
def test_create_user():
    response = client.post("/users", json=user_data)
    assert response.status_code == 201
    assert "id" in response.json()
    assert response.json()["name"] == user_data["name"]

def test_update_user():
    response = client.put("/users/1", json=updated_data)
    assert response.status_code == 200
    assert "id" in response.json()
    assert response.json()["name"] == updated_data["name"]
```

### GOOD: Custom assertion helper

```python
# tests/helpers.py
def assert_successful_user_response(response, expected_data, status_code=200):
    """Assert response is successful user operation."""
    assert response.status_code == status_code
    data = response.json()
    assert "id" in data
    assert data["name"] == expected_data["name"]
    return data

# tests/test_user_endpoints.py
from tests.helpers import assert_successful_user_response

def test_create_user():
    response = client.post("/users", json=user_data)
    assert_successful_user_response(response, user_data, status_code=201)
```

## Pattern 7: Duplicated Test Data (TypeScript/Vitest Example)

### BAD: Repeated mock data structure (real codebase example)

```typescript
// GlobalApprovalsList.test.ts - Pattern repeated 11+ times across tests
it("should display approvals grouped by agent", async () => {
  const mockApprovals = [
    {
      uri: "resource://approvals/1",
      mimeType: "application/json",
      text: JSON.stringify({
        agent_id: "agent-1",
        call_id: "call-1",
        tool: "read_file",
        args: { path: "/test.txt" },
        timestamp: "2025-01-01T00:00:00Z",
      }),
    },
    // ... more approvals
  ];
  mockReadResource.mockResolvedValue(mockApprovals);
  // ... test logic
});

it("should call approve tool when approve button is clicked", async () => {
  const mockApprovals = [
    // ❌ Same structure, 10 more times
    {
      uri: "resource://approvals/1",
      mimeType: "application/json",
      text: JSON.stringify({
        agent_id: "agent-1",
        call_id: "call-1",
        tool: "test_tool",
        args: {},
        timestamp: "2025-01-01T00:00:00Z",
      }),
    },
  ];
  mockReadResource.mockResolvedValue(mockApprovals);
  // ... test logic
});

// Pattern continues in 9 more tests...
```

### GOOD: Extract fixture factory

```typescript
// tests/fixtures/approvals.ts
export function createMockApproval(overrides?: Partial<PendingApproval>) {
  return {
    uri: overrides?.uri ?? "resource://approvals/1",
    mimeType: "application/json",
    text: JSON.stringify({
      agent_id: overrides?.agent_id ?? "agent-1",
      call_id: overrides?.call_id ?? "call-1",
      tool: overrides?.tool ?? "test_tool",
      args: overrides?.args ?? {},
      timestamp: overrides?.timestamp ?? "2025-01-01T00:00:00Z",
    }),
  };
}

export function mockApprovalsResource(approvals: ReturnType<typeof createMockApproval>[]) {
  mockReadResource.mockResolvedValue(approvals);
}

// GlobalApprovalsList.test.ts
import { createMockApproval, mockApprovalsResource } from "./fixtures/approvals";

it("should display approvals grouped by agent", async () => {
  mockApprovalsResource([
    createMockApproval({ agent_id: "agent-1", tool: "read_file" }),
    createMockApproval({ agent_id: "agent-1", tool: "write_file", call_id: "call-2" }),
    createMockApproval({ agent_id: "agent-2", tool: "exec", call_id: "call-3" }),
  ]);
  // ... test logic
});

it("should call approve tool when approve button is clicked", async () => {
  mockApprovalsResource([createMockApproval()]); // Defaults work for this test
  // ... test logic
});
```

## Pattern 8: Duplicated Test Workflows (TypeScript/Vitest Example)

### BAD: Repeated interaction workflow (real codebase example)

```typescript
// GlobalApprovalsList.test.ts - "Open reject dialog" pattern repeated 5+ times
it("should open reject dialog when reject button is clicked", async () => {
  const mockApprovals = [
    /* ... */
  ];
  mockReadResource.mockResolvedValue(mockApprovals);

  const { container } = render(GlobalApprovalsList);

  await waitFor(() => {
    expect(screen.getByText("test_tool")).toBeTruthy();
  });

  // Find and click reject button
  const rejectButton = container.querySelector(".btn-reject");
  expect(rejectButton).toBeTruthy();
  if (rejectButton) {
    await fireEvent.click(rejectButton);
  }

  await waitFor(() => {
    expect(screen.getByText("Reject Tool Call")).toBeTruthy();
  });
});

it("should require rejection reason to be non-empty", async () => {
  const mockApprovals = [
    /* ... */
  ];
  mockReadResource.mockResolvedValue(mockApprovals);

  const { container } = render(GlobalApprovalsList);

  await waitFor(() => {
    expect(screen.getByText("test_tool")).toBeTruthy();
  });

  // ❌ Exact same workflow - repeated 5 times
  const rejectButton = container.querySelector(".btn-reject");
  if (rejectButton) {
    await fireEvent.click(rejectButton);
  }

  await waitFor(() => {
    expect(screen.getByText("Reject Tool Call")).toBeTruthy();
  });

  // Test-specific logic here
});

// Pattern continues in 3 more tests...
```

### GOOD: Extract workflow helper

```typescript
// tests/helpers/interactions.ts
export async function openRejectDialog(container: HTMLElement) {
  const rejectButton = container.querySelector(".btn-reject");
  expect(rejectButton).toBeTruthy();

  if (rejectButton) {
    await fireEvent.click(rejectButton);
  }

  await waitFor(() => {
    expect(screen.getByText("Reject Tool Call")).toBeTruthy();
  });
}

export async function renderWithApproval(approvals: ReturnType<typeof createMockApproval>[] = [createMockApproval()]) {
  mockApprovalsResource(approvals);
  const result = render(GlobalApprovalsList);

  await waitFor(() => {
    expect(screen.getByText(approvals[0].tool ?? "test_tool")).toBeTruthy();
  });

  return result;
}

// GlobalApprovalsList.test.ts
import { openRejectDialog, renderWithApproval } from "./helpers/interactions";

it("should open reject dialog when reject button is clicked", async () => {
  const { container } = await renderWithApproval();
  await openRejectDialog(container);
  // Dialog is now open, test-specific assertions
});

it("should require rejection reason to be non-empty", async () => {
  const { container } = await renderWithApproval();
  await openRejectDialog(container);

  // Test-specific logic
  const confirmButton = container.querySelector(".btn-primary");
  expect(confirmButton?.hasAttribute("disabled")).toBe(true);
});
```

**Benefits of workflow extraction**:

- **Single source of truth**: Dialog opening logic in one place
- **Easier maintenance**: Update selector once if HTML structure changes
- **Test intent clarity**: `openRejectDialog()` is self-documenting
- **Reduced duplication**: 5 tests × 10 lines = 50 lines → 5 tests × 1 line = 5 lines

## Detection Strategy

**MANDATORY Step 0**: Run duplication detection tools on the entire codebase.

- This scan is **required** - do not skip this step
- You **must** read and process ALL duplication candidates using your intelligence
- High recall required, high precision NOT required - you determine which duplications warrant extraction
- Review each for: duplication count (3+ instances?), complexity, likelihood of divergent evolution
- Prevents lazy analysis by forcing examination of ALL concrete duplication candidates

```bash
# 1. Run jscpd to find duplicated code blocks (works for Python, JavaScript, etc.)
jscpd . --min-lines 5 --min-tokens 30 --format "json" > duplication_report.json

# View jscpd summary
cat duplication_report.json | jq '.statistics'

# View duplicated blocks with locations
cat duplication_report.json | jq '.duplicates[] | {format, lines, tokens, firstFile: .firstFile.name, firstStart: .firstFile.start, secondFile: .secondFile.name, secondStart: .secondFile.start}'

# 2. Find most common line.strip() strings (often indicates duplicated assertions/logic)
# Extract all non-empty, non-comment lines, strip whitespace, count frequency
rg --type py --no-heading --no-filename '^[[:space:]]*[^#].*\S' | \
  sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | \
  sort | uniq -c | sort -rn | head -50

# 3. Find exact line matches with file:line locations (top 20 most common)
rg --type py --no-heading '^[[:space:]]*[^#].*\S' | \
  sed 's/^\([^:]*\):\([0-9]*\):[[:space:]]*\(.*\)/\3|\1:\2/' | \
  awk -F'|' '{lines[$1] = lines[$1] $2 ", "} END {for (line in lines) print length(lines[line]), line, substr(lines[line], 1, length(lines[line])-2)}' | \
  sort -rn | head -20
```

**What to review from jscpd output**:

1. **Duplication count**: Does block appear 3+ times?
2. **Complexity**: Is it complex enough to warrant extraction (5+ lines, not trivial)?
3. **Consistency**: Should changes propagate consistently?
4. **Domain**: Test fixtures, business logic, validation, utilities?
5. **Evolution**: Will these likely evolve together or diverge?

**What to review from common line patterns**:

1. **Test assertions**: Same assertion pattern across multiple tests?
2. **Setup/teardown**: Repeated initialization/cleanup?
3. **Validation**: Same validation logic duplicated?
4. **Data transformation**: Same transformation pattern?

**Process ALL output**: Read each duplication candidate, use your judgment to identify extraction opportunities.

---

### 1. AST-Based Detection (Most Reliable)

Build analyzer to find duplicate code patterns:

```python
import ast
from collections import defaultdict

class TestDuplicationDetector(ast.NodeVisitor):
    def __init__(self):
        self.fixtures = defaultdict(list)  # fixture_name -> [(file, code)]
        self.assertion_patterns = defaultdict(list)  # pattern_hash -> [(file, line)]
        self.mock_configs = defaultdict(list)  # mock_target -> [(file, config)]

    def visit_FunctionDef(self, node):
        # Check for @pytest.fixture decorator
        for decorator in node.decorator_list:
            if self._is_pytest_fixture(decorator):
                self.fixtures[node.name].append((self.current_file, ast.unparse(node)))

        # Check for repeated assertion sequences
        assertion_sequence = self._extract_assertion_sequence(node)
        if len(assertion_sequence) >= 3:
            pattern_hash = hash(tuple(assertion_sequence))
            self.assertion_patterns[pattern_hash].append((self.current_file, node.lineno))

        self.generic_visit(node)
```

### 2. Text-Based Pattern Matching

```bash
# Find duplicate fixture names
find tests -name "*.py" -exec grep -H "^@pytest.fixture" {} \; | \
    sed 's/.*def \(\w\+\).*/\1/' | sort | uniq -c | sort -rn | awk '$1 > 1'

# Find duplicate assertion helpers
rg --type py "^def assert_\w+" tests/ | \
    cut -d: -f2 | sort | uniq -c | sort -rn | awk '$1 > 1'

# Find duplicate mock patches
rg --type py "@patch\(\"[^\"]+\"\)" tests/ -o | sort | uniq -c | sort -rn | awk '$1 > 2'
```

### 3. Semantic Similarity (Advanced)

Use AST comparison to find semantically similar but textually different code:

```python
def ast_similarity(node1: ast.AST, node2: ast.AST) -> float:
    """Calculate similarity score between two AST nodes."""
    # Strip variable names, keep structure
    norm1 = normalize_ast(node1)
    norm2 = normalize_ast(node2)
    return structural_similarity(norm1, norm2)

def find_similar_fixtures(threshold=0.8):
    """Find fixtures with similar implementation but different names."""
    fixtures = collect_all_fixtures()
    for f1, f2 in combinations(fixtures, 2):
        if ast_similarity(f1.ast, f2.ast) > threshold:
            yield (f1, f2, "Similar fixtures - consider consolidation")
```

## Grep Patterns (Quick High-Recall Scan)

```bash
# Find repeated fixture definitions
find tests -name "*.py" -print0 | xargs -0 grep -h "^def \w\+(" | sort | uniq -c | sort -rn

# Find repeated mock setups
rg --type py "mock\.\w+\.return_value\s*=" tests/ | cut -d: -f2- | sort | uniq -c | sort -rn

# Find repeated parametrize data
rg --type py "@pytest.mark.parametrize" tests/ -A 5 | grep "\[" | sort | uniq -c | sort -rn

# Find repeated assertion sequences (common 3-line patterns)
rg --type py -U "assert .+\n.*assert .+\n.*assert .+" tests/ | sort | uniq -c | sort -rn
```

## Tools for Finding Duplication

### 1. pytest-patterns Plugin (Hypothetical)

```bash
pytest --collect-only --show-duplicate-fixtures
pytest --collect-only --show-duplicate-setup
```

### 2. Custom Pytest Plugin

```python
# conftest.py
def pytest_collection_modifyitems(session, config, items):
    """Analyze collected tests for duplication patterns."""
    fixtures_by_name = defaultdict(list)
    for item in items:
        for fixture_name in item.fixturenames:
            fixtures_by_name[fixture_name].append(item.nodeid)

    # Report fixtures used across multiple modules
    for name, locations in fixtures_by_name.items():
        if len(set(loc.split("::")[0] for loc in locations)) > 2:
            print(f"Fixture '{name}' used in {len(locations)} places - consider conftest.py")
```

### 3. Code Clone Detection Tools

```bash
# PMD CPD (Copy-Paste Detector)
pmd cpd --minimum-tokens 30 --files tests/ --language python

# jscpd (JavaScript Copy Paste Detector, works for Python too)
jscpd tests/ --min-lines 5 --min-tokens 30
```

## Manual Review Process

1. **Identify duplication candidates** using automated tools
2. **Assess extraction value**:
   - Used in 3+ test modules? → Extract to conftest.py
   - Complex setup (10+ lines)? → Extract to fixture
   - Domain-specific assertions? → Create custom matcher
3. **Choose appropriate scope**:
   - `tests/conftest.py` → Global (all tests)
   - `tests/unit/conftest.py` → Directory-scoped
   - `tests/helpers.py` → Import explicitly (non-fixture helpers)
4. **Extract and refactor**:
   - Move to appropriate location
   - Add docstring explaining purpose and usage
   - Update all call sites
   - Run tests to ensure behavior unchanged

## When NOT to Extract

### 1. **Test-Specific Context Needed**

```python
# Keep local - test-specific user setup
def test_admin_permissions():
    admin = User(id=1, role="admin", permissions=["*"])
    assert admin.can_delete_users()

def test_regular_user_permissions():
    user = User(id=2, role="user", permissions=["read"])
    assert not user.can_delete_users()
```

### 2. **Clarity Sacrificed**

```python
# BAD: Over-abstracted fixture
@pytest.fixture
def configured_service(db, cache, logger, config, metrics):
    return Service(db, cache, logger, config, metrics)

# BETTER: Explicit setup in test
def test_service_behavior():
    service = Service(
        db=mock_db,
        cache=mock_cache,
        logger=test_logger,
        config=test_config,
        metrics=test_metrics,
    )
    # Clear what's being tested
```

### 3. **Evolution Likely to Diverge**

```python
# Keep separate - different evolution paths
# tests/test_api_v1.py
@pytest.fixture
def v1_user():
    return {"id": 1, "name": "Test"}  # V1 format

# tests/test_api_v2.py
@pytest.fixture
def v2_user():
    return {"user_id": 1, "full_name": "Test"}  # V2 format (will evolve differently)
```

## Benefits

✅ **Reduced maintenance**: Fix bugs/update patterns in one place
✅ **Consistency**: All tests use same setup/assertion patterns
✅ **Discoverability**: Easier to find existing test helpers
✅ **Less code**: Fewer lines to read and maintain
✅ **Better signal-to-noise**: Test logic stands out from boilerplate

## References

- [Pytest Fixtures Documentation](https://docs.pytest.org/en/stable/fixture.html)
- [PyHamcrest Custom Matchers](https://github.com/hamcrest/PyHamcrest)
- [Refactoring Test Code](https://martinfowler.com/articles/refactoring-test-code.html)

## Integration with CI

Add duplication detection to pre-commit or CI:

```yaml
# .github/workflows/test-quality.yml
- name: Check test code duplication
  run: |
    python scripts/detect_test_duplication.py --threshold 0.7
    # Fails if duplication score exceeds threshold
```
