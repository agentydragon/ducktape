# Ground Truth Versioning for Specimens

## Problem Statement

When a specimen's ground truth changes (issues added/removed/modified, line ranges adjusted), evaluation runs based on the old ground truth become potentially misleading:

- **Accuracy metrics become incomparable** - a run that scored 0.85 against 10 issues is fundamentally different from one that scored 0.85 against 15 issues
- **Historical analysis is ambiguous** - did the model improve, or did we make the task easier?
- **Aggregate queries are unreliable** - cross-specimen averages mix runs against different ground truths
- **Debugging is harder** - when reviewing a low-scoring run, you can't easily tell if the ground truth has changed since

## Design Goals

1. **Preserve historical data** - don't lose old evaluation runs
2. **Query simplicity by default** - most queries should "just work" without versioning awareness
3. **Enable historical analysis** - support queries like "how did scores change when we fixed the ground truth?"
4. **Incremental complexity** - start simple, allow evolution
5. **Idempotent syncs** - reverting ground truth to a previous state should be handled gracefully

## Option 1: Delete Stale Runs (Simplest)

### Approach

On sync, if ground truth changes:

```python
if specimen_ground_truth_changed(specimen):
    db.execute("DELETE FROM evaluation_runs WHERE specimen_id = ?", specimen.id)
```

### Pros

- Extremely simple
- No schema changes
- No query complexity
- Always shows current results only

### Cons

- **Data loss** - can't analyze historical trends
- **Expensive re-evaluation** - must re-run all experiments after ground truth fixes
- **No audit trail** - can't answer "what did the model produce against the old ground truth?"
- **Breaks ongoing experiments** - if you're iterating on prompts and fix ground truth mid-stream, all context is lost

### Verdict

❌ **Not recommended** - data loss is too costly for research/optimization workflows

## Option 2: Soft Delete with `deleted_at` (Simple)

### Schema Changes

```sql
ALTER TABLE evaluation_runs ADD COLUMN deleted_at TIMESTAMP;
CREATE INDEX idx_eval_runs_not_deleted ON evaluation_runs(specimen_id) WHERE deleted_at IS NULL;
```

### Approach

```python
if specimen_ground_truth_changed(specimen):
    db.execute(
        "UPDATE evaluation_runs SET deleted_at = ? WHERE specimen_id = ? AND deleted_at IS NULL",
        datetime.now(), specimen.id
    )
```

### Query Pattern

```sql
-- Default: active runs only
SELECT * FROM evaluation_runs
WHERE specimen_id = ? AND deleted_at IS NULL

-- Include historical
SELECT * FROM evaluation_runs
WHERE specimen_id = ?
```

### Pros

- Simple to implement (one column, standard pattern)
- Data preserved for forensics
- Partial index keeps current queries fast
- Easy to understand ("deleted" is intuitive)

### Cons

- **No versioning semantics** - can't group by ground truth version
- **No idempotency** - if you revert ground truth to a previous state, old runs stay deleted
- **Ambiguous deletions** - can't distinguish "ground truth changed" from "user manually deleted bad run"
- **Limited analysis** - hard to answer "compare v1 vs v2 ground truth"

### Verdict

✅ **Viable for MVP** if you don't need version-aware analysis

## Option 3: Content-Based Versioning with Hash + Flag (Recommended)

### Schema Changes

```sql
-- Specimens table
ALTER TABLE specimens ADD COLUMN ground_truth_hash TEXT;
CREATE INDEX idx_specimens_gt_hash ON specimens(ground_truth_hash);

-- Evaluation runs table
ALTER TABLE evaluation_runs ADD COLUMN ground_truth_hash TEXT NOT NULL;
ALTER TABLE evaluation_runs ADD COLUMN is_current BOOLEAN NOT NULL DEFAULT true;
CREATE INDEX idx_eval_runs_current ON evaluation_runs(specimen_id, is_current);
CREATE INDEX idx_eval_runs_gt_hash ON evaluation_runs(ground_truth_hash);
```

### Hash Computation

```python
def compute_ground_truth_hash(specimen: Specimen) -> str:
    """Stable hash of issues + ranges (semantic content)."""
    # Sort issues by ID, normalize ranges
    canonical = {
        "issues": sorted([
            {
                "id": issue.id,
                "occurrences": sorted([
                    {
                        "files": sorted([
                            {"path": path, "ranges": sorted(ranges)}
                            for path, ranges in occ.files.items()
                        ], key=lambda x: x["path"])
                    }
                    for occ in issue.occurrences
                ], key=lambda x: str(x["files"]))
            }
            for issue in specimen.issues
        ], key=lambda x: x["id"])
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()
    ).hexdigest()[:16]  # 16 hex chars = 64 bits
```

### Sync Logic

```python
def sync_specimen(specimen: Specimen):
    new_hash = compute_ground_truth_hash(specimen)
    old_hash = db.get_specimen_hash(specimen.id)

    if new_hash != old_hash:
        logger.info(f"Ground truth changed for {specimen.id}: {old_hash} -> {new_hash}")

        # Mark old runs as stale
        db.execute(
            "UPDATE evaluation_runs SET is_current = false "
            "WHERE specimen_id = ? AND ground_truth_hash = ?",
            specimen.id, old_hash
        )

        # Check if we're reverting to a previous version
        reverted_runs = db.execute(
            "UPDATE evaluation_runs SET is_current = true "
            "WHERE specimen_id = ? AND ground_truth_hash = ?",
            specimen.id, new_hash
        ).rowcount

        if reverted_runs:
            logger.info(f"Reactivated {reverted_runs} runs matching previous ground truth")

        # Update specimen hash
        db.execute(
            "UPDATE specimens SET ground_truth_hash = ? WHERE id = ?",
            new_hash, specimen.id
        )
```

### Query Patterns

```sql
-- Default: current runs only (simple, fast)
SELECT * FROM evaluation_runs
WHERE specimen_id = ? AND is_current = true

-- Historical trend analysis
SELECT
    ground_truth_hash,
    COUNT(*) as num_runs,
    AVG(precision) as avg_precision,
    AVG(recall) as avg_recall,
    MIN(created_at) as first_seen,
    MAX(created_at) as last_seen
FROM evaluation_runs
WHERE specimen_id = ?
GROUP BY ground_truth_hash
ORDER BY MIN(created_at)

-- Detect specimens with ground truth churn
SELECT
    specimen_id,
    COUNT(DISTINCT ground_truth_hash) as num_versions
FROM evaluation_runs
GROUP BY specimen_id
HAVING num_versions > 1

-- Compare specific versions
SELECT
    r1.prompt_id,
    AVG(r1.precision) as v1_precision,
    AVG(r2.precision) as v2_precision,
    AVG(r2.precision) - AVG(r1.precision) as delta
FROM evaluation_runs r1
JOIN evaluation_runs r2
    ON r1.specimen_id = r2.specimen_id
    AND r1.prompt_id = r2.prompt_id
WHERE r1.ground_truth_hash = ? AND r2.ground_truth_hash = ?
GROUP BY r1.prompt_id
```

### Pros

- **No data loss** - all runs preserved
- **Simple default queries** - just add `is_current = true`
- **Idempotent** - reverting ground truth reactivates old runs
- **Version-aware analysis** - group/compare by hash
- **Detects no-op changes** - reformatting without semantic change keeps same hash
- **Incremental** - can add explicit version table later without breaking existing code
- **Audit trail** - see exactly when and how often ground truth changed

### Cons

- **Hash collisions** (extremely unlikely with 64-bit hash)
- **Doesn't capture "why"** - no metadata about what changed
- **Hash computation cost** (mitigated: only on sync, can cache)

### Verdict

✅ **Recommended** - best balance of simplicity and capability

## Option 4: Explicit Versioning Table (Most Flexible)

### Schema

```sql
CREATE TABLE specimen_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    ground_truth_hash TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    change_summary TEXT,  -- "Added 3 issues", "Fixed line ranges in issue-042"
    parent_version_id INTEGER REFERENCES specimen_versions(id),
    UNIQUE(specimen_id, version_number),
    UNIQUE(specimen_id, ground_truth_hash)
);

ALTER TABLE specimens ADD COLUMN current_version_id INTEGER REFERENCES specimen_versions(id);
ALTER TABLE evaluation_runs ADD COLUMN specimen_version_id INTEGER REFERENCES specimen_versions(id);

CREATE INDEX idx_specimen_versions_specimen ON specimen_versions(specimen_id);
CREATE INDEX idx_eval_runs_version ON evaluation_runs(specimen_version_id);
```

### Sync Logic

```python
def sync_specimen(specimen: Specimen):
    new_hash = compute_ground_truth_hash(specimen)
    current_version = db.get_current_version(specimen.id)

    if current_version and new_hash == current_version.ground_truth_hash:
        return  # No change

    # Check for revert to existing version
    existing_version = db.get_version_by_hash(specimen.id, new_hash)

    if existing_version:
        logger.info(f"Reverting {specimen.id} to version {existing_version.version_number}")
        db.execute(
            "UPDATE specimens SET current_version_id = ? WHERE id = ?",
            existing_version.id, specimen.id
        )
    else:
        # Create new version
        next_version_num = (current_version.version_number + 1) if current_version else 1
        change_summary = compute_change_summary(current_version, specimen)

        version_id = db.execute(
            "INSERT INTO specimen_versions "
            "(specimen_id, version_number, ground_truth_hash, change_summary, parent_version_id) "
            "VALUES (?, ?, ?, ?, ?)",
            specimen.id, next_version_num, new_hash, change_summary,
            current_version.id if current_version else None
        ).lastrowid

        db.execute(
            "UPDATE specimens SET current_version_id = ? WHERE id = ?",
            version_id, specimen.id
        )
```

### Query Patterns

```sql
-- Current runs (join required)
SELECT r.* FROM evaluation_runs r
JOIN specimens s ON r.specimen_id = s.id
WHERE r.specimen_id = ? AND r.specimen_version_id = s.current_version_id

-- Version timeline
SELECT
    v.version_number,
    v.created_at,
    v.change_summary,
    COUNT(r.id) as num_runs,
    AVG(r.precision) as avg_precision
FROM specimen_versions v
LEFT JOIN evaluation_runs r ON r.specimen_version_id = v.id
WHERE v.specimen_id = ?
GROUP BY v.id
ORDER BY v.version_number

-- Diff between versions
SELECT * FROM specimen_versions
WHERE specimen_id = ?
ORDER BY version_number
```

### Pros

- **Rich metadata** - capture why ground truth changed
- **Version graph** - parent_version_id enables branching/merging semantics
- **Clear semantics** - explicit version numbers are intuitive
- **Flexible analysis** - can query by version number, hash, or date

### Cons

- **Most complex** - requires joins, version management logic
- **Slower queries** - default queries need join to specimens table
- **Migration complexity** - backfilling version history is harder
- **Overkill?** - do we really need branching/metadata?

### Verdict

✅ **Use if** you need rich version metadata or plan to branch/merge ground truths
❌ **Skip for MVP** - start with Option 3 and migrate later if needed

## Migration Path: Option 3 → Option 4

If you start with Option 3 (hash + flag) and later need Option 4 (explicit versions):

```sql
-- Backfill versions table from existing runs
INSERT INTO specimen_versions (specimen_id, version_number, ground_truth_hash, created_at)
SELECT
    specimen_id,
    ROW_NUMBER() OVER (PARTITION BY specimen_id ORDER BY MIN(created_at)) as version_number,
    ground_truth_hash,
    MIN(created_at) as created_at
FROM evaluation_runs
GROUP BY specimen_id, ground_truth_hash;

-- Link runs to versions
UPDATE evaluation_runs
SET specimen_version_id = (
    SELECT v.id FROM specimen_versions v
    WHERE v.specimen_id = evaluation_runs.specimen_id
      AND v.ground_truth_hash = evaluation_runs.ground_truth_hash
);

-- Set current version
UPDATE specimens
SET current_version_id = (
    SELECT id FROM specimen_versions
    WHERE specimen_id = specimens.id
      AND ground_truth_hash = specimens.ground_truth_hash
);
```

This is clean because Option 3 already has the hash, which is the stable identifier.

## Recommendation

**Start with Option 3 (Content-Based Versioning):**

1. **Phase 1 (MVP)**: Implement hash + `is_current` flag
   - Add columns to specimens and evaluation_runs
   - Update sync logic to mark stale runs
   - Update default queries to filter `is_current = true`
   - Backfill hashes for existing data

2. **Phase 2 (If needed)**: Add version metadata
   - Migrate to explicit specimen_versions table
   - Preserve hash as stable identifier
   - Add change summaries, parent links as needed

3. **Phase 3 (If needed)**: Add UI/tooling
   - Version comparison views
   - Ground truth diff visualization
   - Change impact analysis

## Implementation Checklist

### Database Schema

- [ ] Add `specimens.ground_truth_hash` column
- [ ] Add `evaluation_runs.ground_truth_hash` column (NOT NULL)
- [ ] Add `evaluation_runs.is_current` column (BOOLEAN NOT NULL DEFAULT true)
- [ ] Add indexes on (specimen_id, is_current) and ground_truth_hash
- [ ] Write migration with backfill logic

### Core Logic

- [ ] Implement `compute_ground_truth_hash()` function
- [ ] Update specimen sync to detect hash changes
- [ ] Update sync to mark old runs `is_current = false`
- [ ] Update sync to reactivate runs if hash matches previous version
- [ ] Log ground truth changes (specimen ID, old hash, new hash)

### Query Updates

- [ ] Add `is_current = true` to default evaluation_runs queries
- [ ] Update aggregate functions (avg, percentile) to filter current runs
- [ ] Add CLI flag `--include-stale` to opt into historical data

### Testing

- [ ] Test hash stability (same ground truth → same hash)
- [ ] Test hash changes (modified issue → different hash)
- [ ] Test idempotency (revert ground truth → old runs reactivated)
- [ ] Test no-op changes (reformatting → same hash)
- [ ] Test query performance with stale runs

### Documentation

- [ ] Document versioning behavior in props README
- [ ] Add query examples for version-aware analysis
- [ ] Document backfill procedure for existing databases

## Open Questions

1. **Hash collision handling**: Should we add a secondary check (compare actual issues) if hash matches but content differs?
   - Proposal: Log a warning, keep both hashes, let user manually resolve

2. **Cascade on specimen deletion**: Should deleting a specimen delete all runs, or soft-delete?
   - Proposal: Hard delete (specimen is the entity boundary)

3. **UI affordances**: How should we surface "stale run" warnings in the UI?
   - Proposal: Badge on run cards, filter toggle in list views

4. **Performance**: With 1000s of runs per specimen, does `is_current` index cover slow queries?
   - Proposal: Monitor, consider partitioning by specimen_id if needed

5. **Cross-specimen queries**: Should aggregate functions (e.g., "average precision across all specimens") mix ground truth versions?
   - Proposal: Default to current only, add explicit flag for cross-version aggregation
