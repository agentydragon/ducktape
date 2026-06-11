# Step Runner Fixture Extraction Analysis

**Date**: 2025-12-01
**Task**: Extract reusable agent fixtures from test files
**Analysis Based On**: `/code/gitlab.com/agentydragon/ducktape/adgn/docs/test_patterns_analysis.md`

## Executive Summary

After comprehensive analysis of 14 test files using `make_step_runner`, I found:

- **NO new fixtures should be created** at this time
- Existing fixtures are appropriately scoped
- Most step sequences are test-specific (correct design)
- The one duplicated pattern has legitimate variations

## Patterns Analyzed

### Pattern 1: Echo + Done (4 occurrences)

**Locations**:

1. `tests/agent/test_approval_integration.py`: `MakeCall("echo", "echo", EchoInput(text="test"))` + `AssistantMessage("done")`
2. `tests/agent/test_with_mocks.py`: `MakeCall("echo", "echo", EchoInput(text="hi"))` + `AssistantMessage("done")`
3. `tests/agent/test_agent_mcp_echo.py`: `MakeCall("echo", "echo", EchoInput(text="hello"))` + `AssistantMessage("done")`
4. `tests/e2e/test_approvals.py`: `MakeCall("echo", "echo", {"text": "hello"})` + `MakeCall("ui", "end_turn", {})`

**Why NOT extract**:

- **Different echo text**: Each test uses different text (`"test"`, `"hi"`, `"hello"`) which may be intentional for test clarity
- **Different endings**: First 3 use `AssistantMessage("done")`, last uses `MakeCall("ui", "end_turn", {})` (e2e pattern)
- **Different typing**: Last uses dict args (legacy pattern from e2e test)
- **Only 3 true duplicates**: The e2e variant is different enough to be its own pattern
- **High locality**: All in agent test suite, likely intentionally similar but not identical

**Verdict**: **Do not extract** - variations are intentional, and creating a parameterized fixture would be over-engineering.

### Pattern 2: UI End Turn (3 occurrences in e2e/)

**Locations**:

- `tests/e2e/test_approvals.py`
- `tests/e2e/test_proposals_reject.py`
- `tests/e2e/test_ui.py`

**Why NOT extract**:

- All are **different first steps** (echo, policy_proposer, send_message)
- Only commonality is ending with `end_turn`
- E2E tests are integration tests - each scenario is unique
- Creating a factory would require so many parameters it would lose clarity

**Verdict**: **Do not extract** - each e2e scenario is unique, sharing only the UI server convention.

### Pattern 3: Critic Workflow (Already extracted)

**Location**: `tests/props/prompt_eval/test_prompt_optimizer_integration.py` lines 124-137

```python
@pytest.fixture
def critic_agent_steps():
    """Declarative steps for Critic agent - reports issues."""
    return [
        MakeCall("critic_submit", "upsert_issue", UpsertIssueInput(...)),
        CheckThenCall("critic_submit_upsert_issue", "critic_submit", "add_occurrence", ...),
        CheckThenCall("critic_submit_add_occurrence", "critic_submit", "submit", SubmitInput(issues=1)),
    ]
```

**Usage**: Only used in ONE test file (the integration test)

**Verdict**: **Correctly scoped** - This is a test-specific fixture for the prompt optimizer integration test. It should NOT move to conftest since it's not reused elsewhere.

### Pattern 4: PO Workflow (Already extracted)

**Location**: `tests/props/prompt_eval/test_prompt_optimizer_integration.py` lines 91-120

```python
@pytest.fixture
def po_agent_steps():
    """Declarative steps for PO agent - prompt optimization workflow."""
    return [
        MakeCall("docker", "exec", ExecInput(...)),
        CheckThenCall("docker_exec", "prompt_eval", "upsert_prompt", UpsertPromptInput(...)),
        ExtractThenCall("prompt_eval_upsert_prompt", UpsertPromptOutput, lambda ...),
        ExtractThenCall("prompt_eval_run_critic", RunCriticOutput, lambda ...),
        Finish("prompt_eval_run_grader", message="Done"),
    ]
```

**Usage**: Only used in ONE test file (the integration test)

**Verdict**: **Correctly scoped** - Same as critic workflow. Test-specific, not cross-test.

### Pattern 5: Single Message (2 occurrences)

**Locations**:

- `tests/llm/cli/test_llm_edit_cli.py`: `steps=[AssistantMessage("ok")]`
- `tests/mcp/approval_policy/test_server_available.py`: `steps=[AssistantMessage("I can see the approval tools")]`

**Why NOT extract**:

- **Trivial pattern**: One line each
- **Different messages**: Not truly duplicated
- **Creating a fixture is overkill**: `make_step_runner(steps=[AssistantMessage("ok")])` is already minimal

**Verdict**: **Do not extract** - creating a fixture for a 1-line pattern adds complexity without value.

### Pattern 6: Unique Sequences (8 occurrences)

**Files with unique step sequences**:

- `test_flat_tools_schema.py` - Tests schema changes across phases (2 sequences, similar but phase-specific)
- `test_mcp_resources_flow.py` - Resources read test (unique)
- `test_notifications_handler.py` - Approval policy notification test (unique)
- `test_proposals_reject.py` - Policy proposal rejection (unique)
- `test_ui.py` - UI multi-turn test (unique)
- `test_lint_issue_bootstrap.py` - Lint controller bootstrap (unique)

**Verdict**: **All correctly designed** - Each test has its own unique scenario. No reuse opportunities.

## Fixture Scope Analysis

### Current State (Correct)

| Fixture Location                                               | Fixtures                                                     | Usage Scope        | Status     |
| -------------------------------------------------------------- | ------------------------------------------------------------ | ------------------ | ---------- |
| `tests/conftest.py`                                            | `make_step_runner`                                           | Global (all tests) | ✅ Correct |
| `tests/support/responses.py`                                   | `ResponsesFactory`, `_StepRunner`                            | Global             | ✅ Correct |
| `tests/support/steps.py`                                       | Step classes (`MakeCall`, `CheckThenCall`, etc.)             | Global             | ✅ Correct |
| `tests/props/prompt_eval/test_prompt_optimizer_integration.py` | `po_agent_steps`, `critic_agent_steps`, `grader_agent_steps` | Single test file   | ✅ Correct |
| `tests/agent/conftest.py`                                      | `make_test_agent`, policy fixtures, etc.                     | Agent suite        | ✅ Correct |

### What's Missing? (Answer: Nothing)

The analysis doc suggests creating fixtures like:

```python
@pytest.fixture
def po_agent(make_step_runner, po_agent_steps) -> _StepRunner:
    """Ready-to-use PO agent runner."""
    return make_step_runner(steps=po_agent_steps)
```

**Why this is NOT needed**:

1. **Single use**: These fixtures are used in ONE test
2. **Clear inline**: `po_runner = make_step_runner(steps=po_agent_steps)` is already clear
3. **No parameterization**: The steps fixture already exists; wrapping it adds no value
4. **Premature abstraction**: YAGNI principle - don't extract until 2nd use

## Anti-Patterns to Avoid

### ❌ Don't Extract One-Off Patterns

```python
# BAD: Used in only one test
@pytest.fixture
def echo_hello_done_agent(make_step_runner):
    return make_step_runner(steps=[
        MakeCall("echo", "echo", EchoInput(text="hello")),
        AssistantMessage("done"),
    ])
```

### ❌ Don't Over-Parameterize

```python
# BAD: Too many parameters = not reusable
@pytest.fixture
def make_generic_agent(make_step_runner):
    def _make(server, tool, args, ending):
        return make_step_runner(steps=[
            MakeCall(server, tool, args),
            ending,
        ])
    return _make
```

### ❌ Don't Extract Trivial Patterns

```python
# BAD: One line is not worth a fixture
@pytest.fixture
def ok_agent(make_step_runner):
    return make_step_runner(steps=[AssistantMessage("ok")])
```

### ✅ DO Extract When You Have

1. **Exact duplication** across 3+ tests
2. **Stable pattern** (won't change per test)
3. **Domain concept** (e.g., "standard critic workflow")
4. **Clear parameterization** (1-2 meaningful params)

## Recommendations

### 1. Keep Current Structure (No Changes Needed)

The test suite is well-organized:

- Global fixtures in `tests/conftest.py` and `tests/support/`
- Suite-specific fixtures in `tests/agent/conftest.py`, `tests/props/conftest.py`
- Test-specific fixtures in individual test files

### 2. Monitor for Future Opportunities

**Trigger for extraction**: When you write a 3rd test that needs the SAME step sequence, extract then.

**Example scenario where extraction would be appropriate**:

```python
# If 3+ tests all need this EXACT sequence:
steps = [
    MakeCall("critic_submit", "upsert_issue", UpsertIssueInput(issue_id="test", description="desc")),
    CheckThenCall("critic_submit_upsert_issue", "critic_submit", "submit", SubmitInput(issues=1)),
]

# Then extract to tests/props/conftest.py:
@pytest.fixture
def simple_critic_steps():
    """Standard critic workflow: upsert issue + submit."""
    return [...]
```

### 3. Document Pattern Guidelines

Add to `docs/test_patterns_analysis.md`:

```markdown
## When to Extract Step Fixtures

✅ **Extract when**:

- Pattern appears in 3+ tests
- Steps are identical or differ by 1-2 parameters
- Pattern represents a stable domain concept

❌ **Don't extract when**:

- Pattern appears in 1-2 tests
- Pattern requires 3+ parameters
- Pattern is trivial (1-2 steps)
- Tests are intentionally similar but not identical
```

### 4. Consider Future Patterns

If these patterns emerge, extract them:

**Potential future fixture** (if 3+ uses appear):

```python
@pytest.fixture
def make_echo_agent(make_step_runner):
    """Factory for echo agents with custom text."""
    def _make(text: str) -> _StepRunner:
        return make_step_runner(steps=[
            MakeCall("echo", "echo", EchoInput(text=text)),
            AssistantMessage("done"),
        ])
    return _make
```

But **don't create this preemptively** - wait for the 3rd use.

## Metrics

| Metric                               | Count                            | Recommendation            |
| ------------------------------------ | -------------------------------- | ------------------------- |
| Total files using `make_step_runner` | 14                               | ✅ Good adoption          |
| Unique step sequences                | 14                               | ✅ Each test is unique    |
| Exact duplicates (3+ occurrences)    | 0                                | ✅ No extraction needed   |
| Similar patterns (variations)        | 4 (echo+done)                    | ✅ Intentional variations |
| Test-specific fixtures               | 3 (PO, critic, grader)           | ✅ Correctly scoped       |
| Suite-specific fixtures              | Many in conftest.py              | ✅ Appropriate            |
| Global fixtures                      | `make_step_runner`, step classes | ✅ Well-designed          |

## Conclusion

**No fixtures should be extracted at this time.**

The test suite demonstrates **good design**:

1. Global infrastructure (`make_step_runner`, step classes) is reusable
2. Test scenarios are appropriately unique
3. Existing fixtures (PO/critic workflows) are correctly scoped to their test file
4. Similar patterns (echo+done) have intentional variations

The analysis doc's recommendation to extract fixtures was based on a misunderstanding - it conflated "similar patterns exist" with "patterns should be deduplicated". In reality, test scenarios SHOULD be similar but tailored to their specific test goals.

**Recommendation**: Keep current structure. Re-evaluate when/if 3+ tests need IDENTICAL step sequences.
