local I = import '../../specimens/lib.libsonnet';

// iss-029: Move directory creation into SQLitePersistence constructor

I.issueOneOccurrence(
  rationale=|||
    Every caller of SQLitePersistence manually creates parent directories before
    instantiation. This should be handled internally by SQLitePersistence.

    **Current pattern (multiple locations):**
    ```python
    # cli.py:114-115
    db_path.parent.mkdir(parents=True, exist_ok=True)
    persistence = SQLitePersistence(db_path)

    # app.py:152-153
    db_path.parent.mkdir(parents=True, exist_ok=True)
    app.state.persistence = SQLitePersistence(db_path)
    ```

    **Why this is problematic:**
    - Violates DRY: every caller must remember to create directories
    - Error-prone: easy to forget the mkdir call
    - Leaks implementation detail: callers shouldn't know SQLite needs parent dirs
    - Not user-friendly: if you forget mkdir, you get a cryptic SQLite error
    - Multiple call sites: currently 2, could be more in future

    **Correct approach:**
    Move directory creation into SQLitePersistence.__init__:

    ```python
    # In sqlite.py SQLitePersistence.__init__:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        # Ensure parent directory exists
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # ... rest of initialization
    ```

    **Benefits:**
    - Callers just do: `persistence = SQLitePersistence(db_path)`
    - Single responsibility: SQLitePersistence handles its own prerequisites
    - Fail-fast: if path is invalid, constructor raises immediately
    - Consistent behavior: all instances get directory creation
    - Future-proof: new callers don't need to know about mkdir

    **Is this safe?**
    Yes:
    - `mkdir(parents=True, exist_ok=True)` is idempotent - safe to call multiple times
    - No side effects if directory already exists
    - Same behavior as current code, just moved inside the class
    - Constructor is the right place for ensuring prerequisites

    **Call sites to update after fix:**
    1. cli.py:114-115 - remove mkdir, keep just SQLitePersistence call
    2. app.py:152-153 - remove mkdir, keep just SQLitePersistence call
  |||,
  properties=['encapsulation', 'api-design', 'DRY', 'maintainability'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/cli.py': [
      [114, 115],  // Manual mkdir before SQLitePersistence
    ],
    'adgn/src/adgn/agent/server/app.py': [
      [152, 153],  // Manual mkdir before SQLitePersistence
    ],
    'adgn/src/adgn/agent/persist/sqlite.py': [
      [44, 50],  // SQLitePersistence.__init__ - should handle mkdir internally
    ],
  },
)
