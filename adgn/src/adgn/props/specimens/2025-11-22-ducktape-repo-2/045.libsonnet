local I = import '../../specimens/lib.libsonnet';

// iss-045: untyped tuple returns instead of structured types

I.issueOneOccurrence(
  rationale= |||
    The policy persistence methods have non-obvious return types:

    1. get_latest_policy returns tuple[str, int] | None:
    ```python
    async def get_latest_policy(self, agent_id: AgentID) -> tuple[str, int] | None: ...
    ```

    Problems:
    - Tuple unpacking requires remembering the order: `content, id = ...`
    - Unclear what the int represents (policy ID)
    - tuple[str, int] | None requires checking None before unpacking
    - No semantic meaning to the tuple elements

    This should return a typed object like PolicyRecord or ActivePolicy.

    2. set_policy returns int:
    ```python
    async def set_policy(self, agent_id: AgentID, *, content: str) -> int: ...
    ```

    Problems:
    - Not obvious from signature what the int represents (policy ID)
    - Callers must know this is the database-assigned ID
    - No documentation in the signature

    At minimum needs a return type annotation like:
    ```python
    -> int  # Policy ID
    ```

    But better: return a PolicyRecord object with id, content, timestamp, etc.

    Fix - Option 1: Create a PolicyRecord type (preferred):

    ```python
    @dataclass
    class PolicyRecord:
        """Active policy record."""
        id: int
        content: str
        created_at: datetime
        agent_id: AgentID

    class Persistence(Protocol):
        async def get_latest_policy(self, agent_id: AgentID) -> PolicyRecord | None:
            """Get latest active policy, or None if no policy set."""
            ...

        async def set_policy(self, agent_id: AgentID, *, content: str) -> PolicyRecord:
            """Set new policy and return the created record."""
            ...
    ```

    Then callers use:
    ```python
    if policy := await persistence.get_latest_policy(agent_id):
        source = policy.content
        policy_id = policy.id
    ```

    Fix - Option 2: Use NamedTuple (lighter weight):

    ```python
    class PolicyData(NamedTuple):
        content: str
        id: int

    async def get_latest_policy(self, agent_id: AgentID) -> PolicyData | None:
        ...
    ```

    Fix - Option 3: At least document the return type:

    ```python
    async def set_policy(self, agent_id: AgentID, *, content: str) -> int:
        """Set new policy.

        Returns:
            Database-assigned policy ID
        """
        ...
    ```

    Benefits of Option 1:
    - Self-documenting (PolicyRecord.content, PolicyRecord.id)
    - Type-safe
    - Can add fields later without breaking API
    - Clear semantics
    - IDE autocomplete works
  |||,
  properties=['structured-data-over-untyped-mappings', 'type-correctness-and-specificity'],
  filesToRanges={
    'adgn/src/adgn/agent/persist/__init__.py': [
      188,
      189,
    ],
  },
)
