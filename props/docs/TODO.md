# Props TODO

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
