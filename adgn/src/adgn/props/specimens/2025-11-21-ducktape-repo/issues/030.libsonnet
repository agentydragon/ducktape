local I = import '../../specimens/lib.libsonnet';

// iss-030: approvals_pending_global constructs URIs manually instead of using constants

I.issueOneOccurrence(
  rationale=|||
    The `approvals_pending_global` function manually constructs resource URIs using f-strings
    instead of using centralized constants from `_shared/constants.py`. URIs in this codebase
    should be constructed from centralized constants/helpers.

    **Current code (line 407):**
    ```python
    approval_uri = f"resource://agents/{agent_id}/approvals/{approval.call_id}"
    ```

    **Why this is problematic:**

    1. **Violates centralization principle**: The codebase has `adgn/mcp/_shared/constants.py` with
       URI format constants like:
       - `AGENTS_APPROVALS_PENDING_URI_FMT = "resource://agents/{agent_id}/approvals/pending"`
       - `AGENTS_APPROVALS_HISTORY_URI_FMT = "resource://agents/{agent_id}/approvals/history"`

       But individual approval URIs are constructed inline instead of from a constant.

    2. **Inconsistent with codebase pattern**: Other parts of the code use constants:
       ```python
       from adgn.mcp._shared.constants import AGENTS_APPROVALS_PENDING_URI_FMT
       uri = AGENTS_APPROVALS_PENDING_URI_FMT.format(agent_id=agent_id)
       ```

    3. **Hard to change URI patterns**: If the URI scheme changes, must find and update all manual
       constructions instead of changing one constant.

    4. **Risk of typos**: Manual string construction can have typos in the URI pattern that won't
       be caught until runtime.

    5. **No single source of truth**: The URI pattern exists in multiple places instead of one
       canonical definition.

    **Recommended fix:**

    **Step 1**: Add a constant to `adgn/mcp/_shared/constants.py`:
    ```python
    # Individual approval URI (for specific call_id within an agent)
    AGENTS_APPROVAL_URI_FMT: Final[str] = "resource://agents/{agent_id}/approvals/{call_id}"
    ```

    **Step 2**: Import and use the constant:
    ```python
    from adgn.mcp._shared.constants import AGENTS_APPROVAL_URI_FMT

    async def approvals_pending_global():
        # ...
        for agent_id in registry.known_agents():
            # ...
            for approval in pending_approvals:
                approval_uri = AGENTS_APPROVAL_URI_FMT.format(agent_id=agent_id, call_id=approval.call_id)
                # ...
    ```

    **Benefits:**
    - Single source of truth for URI patterns
    - Consistent with codebase conventions
    - Easy to change URI scheme globally
    - No risk of typos in URI construction
    - Self-documenting (constant name shows what the URI is for)
    - Easier to grep for URI usage

    **Note:**
    This pattern should be applied throughout the codebase. Any manual URI construction (using
    f-strings or string concatenation) should be replaced with constants from `_shared/constants.py`.
    If a needed constant doesn't exist, it should be added there rather than constructing the URI inline.
  |||,
  properties=['centralization', 'maintainability', 'constants'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [407, 407],  // Manual URI construction
    ],
    'adgn/src/adgn/mcp/_shared/constants.py': [
      [57, 60],  // Existing agent URI constants (for context)
    ],
  },
)
