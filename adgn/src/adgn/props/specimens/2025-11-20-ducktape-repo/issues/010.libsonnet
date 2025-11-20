local I = import '../../specimens/lib.libsonnet';

// iss-010: Test fixture using plain dict instead of Pydantic and low-value enum assertions

I.issueOneOccurrence(
  rationale=|||
    Two issues in test_mcp_routing.py related to test quality:

    **1. test_tokens fixture uses plain dict instead of Pydantic (lines 15-21):**

    Current code:
    ```python
    @pytest.fixture
    def test_tokens():
        """Override the global TOKEN_TABLE for testing."""
        return {
            "test-human-token": {"role": "human"},
            "test-agent-token": {"role": "agent", "agent_id": "test-agent-1"},
            "test-invalid-role": {"role": "invalid"},
        }
    ```

    Should use Pydantic models for type safety and validation. If there's a TokenConfig or
    similar Pydantic model in the production code, the fixture should construct instances
    of that model rather than plain dicts. This would:
    - Ensure test data matches production types
    - Catch type errors at test construction time
    - Make refactoring safer (Pydantic model changes would break tests immediately)
    - Document the expected structure more clearly

    **2. Low-value enum assertions that duplicate production code (lines 149-150):**

    Current code:
    ```python
    @pytest.mark.asyncio
    async def test_token_role_enum(self):
        """Test TokenRole enum values."""
        assert TokenRole.HUMAN == "human"
        assert TokenRole.AGENT == "agent"

        # Test that enum can be created from string
        role = TokenRole("human")
        assert role == TokenRole.HUMAN
    ```

    The first two assertions (lines 149-150) should be deleted:
    - They just assert the enum values equal their string representations
    - This duplicates what's already in the production code definition
    - Very low value - if someone changes the enum value, they'll see it immediately
    - The assertion doesn't add any meaningful testing

    Lines 152-154 (testing enum construction from string) have more value and can stay,
    as they test actual behavior rather than just duplicating definitions.

    **Recommendation:**
    1. Define or use existing Pydantic model for token structure
    2. Update test_tokens fixture to construct Pydantic model instances
    3. Delete assertions at lines 149-150
  |||,
  properties=['test-quality', 'type-safety', 'pydantic-usage', 'test-value'],
  filesToRanges={
    'adgn/tests/agent/server/test_mcp_routing.py': [
      [15, 21],   // test_tokens fixture using plain dict
      [149, 150], // Low-value enum assertions to delete
    ],
  },
)
