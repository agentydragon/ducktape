# History cutover record

The app-to-service import, handoff and redundant app raw-table retirement are complete.
The importer, its image and the bounded handoff verifier are retired; git history retains
the procedure and tools. Future repair needs separate review, not another importer,
overwrite or whole-history scan.

## Completion and retirement

Testing and staging completed bounded handoff checks: matching inventories, service
coverage of captured raw watermarks and projection coverage for nonempty Sessions.
The operator accepted bounded samples rather than full payload parity and excluded
six empty staging Sessions from further verification. Subsequent runtime cleanup
removed app raw readers/writers, switches and handoff tooling. #9692 replaced
Sandbox-wide projection leases; #9707/#9712 removed app runner-locator routing/storage.

On 2026-10-10 at 07:49 America/Los_Angeles, postflight checks for #9725 found both
primary app databases at `0024_retire_app_raw_history`, with app `event`, `feed_state`,
the rejection function and fence/locator columns absent. Testing was 1/1 and staging
2/2 updated/Ready on `devel-20261010144538-f7d256c`, with no old replicas remaining.
Two active staging checkpoints advanced by 384 and 10 cursor positions between
bounded samples; replacement logs had no matching errors. Service history, public
identities, current app projections and runner storage were outside the deletion scope.
No full-history scan or backfill was repeated. Raw-copy retirement cannot be undone
by Alembic downgrade; rollback requires a pre-retirement backup.

**Staging overlap incident:** #9723 restored RollingUpdate before #9725 deployed.
An old staging replica logged `UndefinedColumnError` for the removed fence column
while the new migration ran. That replica exited and current replicas/projections
recovered; failed-request impact was not quantified. This was not a clean coordinated
stop. Testing retained Recreate. Normal strategies and removal of the temporary test
are complete; the superseded emergency patch is not a remaining operator action.
Future breaking migrations must hold their rollout prerequisite through deployment
verification, not just merge/build completion; automated enforcement is not claimed.
