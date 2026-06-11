# Props TODO

## Snapshot Graders

Low priority improvements (not blocking):

- [ ] Resume existing IN_PROGRESS runs on startup (currently starts fresh)
- [ ] Handle new snapshots added after startup (currently requires restart)

## Ground Truth Write API

Low priority - CLI + YAML workflow is sufficient for now.

- [ ] `POST /api/gt/snapshots/{slug}/issues` - Create new TP/FP
- [ ] `PUT /api/gt/snapshots/{slug}/issues/{id}` - Update TP/FP
- [ ] `DELETE /api/gt/snapshots/{slug}/issues/{id}` - Delete TP/FP
- [ ] `POST /api/gt/snapshots/{slug}/issues/{id}/occurrences` - Add occurrence
- [ ] `DELETE /api/gt/snapshots/{slug}/issues/{id}/occurrences/{occ_id}` - Remove occurrence

Implementation notes:

- Validate against specimen files exist in `snapshot_files`
- Line range validation: `end_line <= file.line_count`
- `pg_notify` triggers already exist - graders will wake automatically
- Consider optimistic locking (etag) for concurrent edits

## Eval Harness

- [ ] Populate evaluation samples with actual IssueEvalSpec instances from git-tracked spec files
  - Select representative specimens and issues
  - Define expectations per occurrence (anchor windows, rationale rubrics, findings matchers)
  - Create IssueEvalSpec instances with OccurrenceCase objects
  - Specs/test cases stay in git (not database)
