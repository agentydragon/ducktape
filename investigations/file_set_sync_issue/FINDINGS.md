# FileSet Sync Issue Investigation

## Problem Statement

Test `test_tp_occ_round_trip` fails with:

```
ValueError: FileSet test-fixtures/train1/d5673969af8b94a23a229e9215d473c4 has no members (referenced by occurrence occ-add)
```

## Git Context

- Branch: `claude/fix-create-code-tar-4BPRQ`
- Merge base with devel: `d5b30c89a38e1929584cab10d6fb057151f41f5f`
- Tests pass on devel, fail on current branch

## Key Observations

### 1. Error IS Raised (Not Swallowed)

The ValueError is properly raised at `props/db/sync/sync.py:566`:

```python
if not restriction:
    raise ValueError(
        f"FileSet {db_occ.snapshot_slug}/{db_occ.match_file_restriction} has no members "
        f"(referenced by occurrence {db_occ.occurrence_id})"
    )
```

Initial question "why was this error swallowed silently?" was based on incomplete log review.

### 2. FileSet Members Created But Not Persisted

From SQL logs during fixture sync (setup phase):

```
INFO props.db.sync.sync: [CURRENT] After creation: FileSet d5673969af8b94a23a229e9215d473c4 has 1 members in session (expected 1)
INFO props.db.sync.sync: [CURRENT] Reusing existing FileSet d5673969af8b94a23a229e9215d473c4, has 1 members
```

But during test execution:

```
INFO props.db.sync.sync: [CURRENT] _reconstruct_occ_common: Found 0 members for d5673969af8b94a23a229e9215d473c4
```

### 3. COUNT Query Shows Members During Sync

The instrumented code runs COUNT queries immediately after creation:

```sql
SELECT count(*) AS count_1
FROM (SELECT file_set_members.snapshot_slug, file_set_members.files_hash, file_set_members.file_path
FROM file_set_members
WHERE file_set_members.snapshot_slug = 'test-fixtures/train1'
  AND file_set_members.files_hash = 'd5673969af8b94a23a229e9215d473c4') AS anon_1
```

Result: 1 member (correct)

But when test queries the same FileSet later: 0 members (incorrect)

## Hypothesis

**Transaction isolation issue**: FileSetMembers are created in one transaction/session context (fixture sync), but not properly committed before the test queries them in a different session context.

### Supporting Evidence

1. The `synced_db` fixture calls `_sync_test_fixtures(db, monkeypatch)` which creates a session
2. That session commits after syncing all fixtures
3. But the test creates a NEW session via `synced_db.session()`
4. If the FileSetMembers weren't flushed/committed properly, they wouldn't be visible in the new session

## Critical Finding: Duplicate FileSet Creation

### Evidence from SQL Logs

The same FileSet (d5673969af8b94a23a229e9215d473c4) is created **multiple times** during fixture sync:

```
INFO props.db.sync.sync: [CURRENT] After creation: FileSet d5673969... has 1 members in session (expected 1)
INFO props.db.sync.sync: [CURRENT] ensure_file_set: slug=test-fixtures/train1, hash=d5673969..., paths=['add.py']
INFO props.db.sync.sync: [CURRENT] Creating new FileSet test-fixtures/train1/d5673969... with 1 paths
```

The pattern repeats: create → verify → **immediately called again** → create again.

### Why Duplicate Creation?

The `ensure_file_set()` function checks for existing FileSet:

```python
existing = session.query(FileSet).filter_by(snapshot_slug=snapshot_slug, files_hash=files_hash).first()
if existing is None:
    # Create new
```

**The SELECT query is not finding the FileSet that was just created and flushed.**

### Multiple INSERT Attempts

SQL logs show the same INSERT being executed multiple times:

```sql
INSERT INTO file_set_members (snapshot_slug, files_hash, file_path)
VALUES ('test-fixtures/train1', 'd5673969af8b94a23a229e9215d473c4', 'add.py')
-- Executed 10+ times according to logs
```

### Primary Key Constraint

FileSetMember has PRIMARY KEY (snapshot_slug, files_hash, file_path). Multiple INSERTs for the same values should cause IntegrityError, but no such error appears in logs.

### Hypothesis: Silent Constraint Violations

Possibility 1: INSERTs are failing with constraint violations but exceptions are being caught somewhere
Possibility 2: Session isolation is preventing SELECT from seeing flushed data
Possibility 3: Something is expunging/rolling back the FileSet between flush and next SELECT

## Root Cause Identified

### Test: FileSet Persists But Members Don't

Created `test_fileset_persistence.py` to check database state after fixture sync:

```python
# Query for FileSet d5673969af8b94a23a229e9215d473c4
fs = session.query(FileSet).filter_by(...).first()
assert fs is not None  # ✅ PASSES

# Query for FileSetMembers
members = session.query(FileSetMember).filter_by(...).all()
assert len(members) > 0  # ❌ FAILS
```

**Result**: `AssertionError: FileSet exists but has ZERO members!`

### What This Means

1. ✅ **FileSet rows persist correctly** - they exist in DB after fixture sync
2. ❌ **FileSetMember rows do NOT persist** - even though they were inserted and verified during sync

The sync logs show:

- `INSERT INTO file_set_members` executes
- Immediately after: COUNT query shows "1 member in session"
- Later: Query finds 0 members in database

**Conclusion**: FileSetMembers are created in the session but rolled back before commit.

## Latest Investigation - Session Boundary Issue

### Added Logging at Line 729

```python
if existing is not None:
    member_count_immediate = session.query(FileSetMember).filter_by(...).count()
    logger.info(f"Found existing FileSet {files_hash}, immediate member count: {member_count_immediate}")
```

**Result**: EVERY query shows "immediate member count: 1" throughout the entire sync!

### Critical Timeline

1. **During Sync (Setup Phase)**: Members exist
   - Every reuse of d5673969 FileSet shows 1 member
   - `INSERT INTO file_set_members` executes
   - FLUSH happens
   - COUNT queries show 1 member
   - COMMIT happens after all specimens synced

2. **After Sync Completes**: Members disappear
   - New test session queries FileSet: ✅ exists
   - New test session queries FileSetMembers: ❌ ZERO members

### Hypothesis: Post-COMMIT Deletion

The members exist throughout sync and survive FLUSH, but disappear AFTER COMMIT and BEFORE the test runs.

Possible causes:

1. Database trigger that runs on COMMIT
2. `delete-orphan` cascade executing during session cleanup (after COMMIT)
3. Some deferred constraint or FK cascade

### Attempted Fix: Remove delete-orphan

Changed line 647 in `props/db/models.py`:

```python
# Before
cascade="all, delete-orphan"

# After
cascade="all"
```

**Result**: No effect - still failing with same error.

## Next Steps

1. ✅ Verify error is raised (not swallowed) - ERROR IS RAISED CORRECTLY
2. ✅ Trace session boundaries - single session used throughout fixture sync
3. ✅ Check for duplicate creation - FOUND: FileSet created 20+ times (reuse pattern)
4. ✅ Test persistence after sync - FileSet exists, members don't
5. ✅ Track member existence during sync - Members exist until COMMIT
6. ✅ Try removing delete-orphan - No effect
7. ⏳ Check database triggers on COMMIT
8. ⏳ Check if issue occurs on devel branch
9. ⏳ Add SQLAlchemy event logging for DELETE operations

## Relevant Code Locations

- `props/db/sync/sync.py:696-727` - `ensure_file_set()` function
- `props/db/sync/sync.py:542-569` - `_reconstruct_occ_common()` function
- `props/testing/fixtures/db.py:165-176` - `_sync_test_fixtures()` function
- `props/db/database.py:116-130` - `Database.session()` context manager
