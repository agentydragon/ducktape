local I = import '../../specimens/lib.libsonnet';

// iss-019: EventRecord and TypedPayload import should not have noqa and should be at top

I.issueOneOccurrence(
  rationale=|||
    The import `from .events import EventRecord, TypedPayload  # noqa: E402` should not use
    `# noqa: E402` and should be moved to the top of the file with other imports. If circular
    import issues prevent this, the module structure should be refactored.

    **Current code (line 130):**
    ```python
    from .events import EventRecord, TypedPayload  # noqa: E402
    ```

    **Why this is problematic:**

    1. **Suppresses legitimate linting error**: `E402` means "module level import not at top of file".
       This is a real code smell that should be fixed, not suppressed.

    2. **Violates PEP 8**: Python style guide says imports should be at the top of the file, after
       module docstring and before module-level code.

    3. **Hides potential circular import**: The `# noqa` suggests there's a circular dependency issue
       that's being papered over rather than properly resolved.

    4. **Inconsistent with rest of file**: All other imports (lines 1-10) are at the top where they belong.

    5. **Makes code harder to understand**: Imports scattered throughout a file make it difficult to see
       all dependencies at a glance.

    **Current import structure:**
    ```python
    # Lines 1-10: Normal imports at top
    from __future__ import annotations
    from datetime import datetime
    from enum import StrEnum
    # ... etc ...

    # Lines 16-127: Type definitions and protocol

    # Line 130: Late import with noqa suppression
    from .events import EventRecord, TypedPayload  # noqa: E402
    ```

    **Recommended approach:**

    **Option 1 (Preferred)**: If this is a circular import issue, refactor to break the cycle:
    - Move EventRecord and TypedPayload to a separate `types.py` module
    - Have both `__init__.py` and `events.py` import from `types.py`
    - This breaks the circular dependency

    **Option 2**: If no circular dependency exists, simply move the import to the top:
    ```python
    from __future__ import annotations

    from datetime import datetime
    from enum import StrEnum
    from typing import Protocol
    from uuid import UUID

    from fastmcp.mcp_config import MCPConfig
    from mcp import types as mcp_types
    from pydantic import BaseModel, ConfigDict, JsonValue

    from adgn.agent.models.proposal_status import ProposalStatus
    from adgn.agent.types import AgentID, ToolCall

    from .events import EventRecord, TypedPayload  # No more noqa!
    ```

    **Option 3**: If TYPE_CHECKING is needed for forward references:
    ```python
    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from .events import EventRecord, TypedPayload
    ```

    **Benefits:**
    - Follows PEP 8 style guidelines
    - Makes all dependencies visible at the top of the file
    - Removes linter suppression (cleaner code)
    - Forces proper resolution of any circular dependencies
    - More maintainable and easier to understand

    **Investigation needed:**
    Check if there's actually a circular import by trying to move the import to the top. If it fails,
    refactor the module structure to break the cycle properly rather than suppressing the warning.
  |||,
  properties=['code-organization', 'imports', 'pep8'],
  filesToRanges={
    'adgn/src/adgn/agent/persist/__init__.py': [
      [130, 130],  // Late import with noqa suppression
    ],
  },
)
