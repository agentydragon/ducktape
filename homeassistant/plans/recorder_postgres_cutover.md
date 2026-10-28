# Recorder cutover to Postgres

The importer and cutover are implemented in [#11795](https://github.com/agentydragon/ducktape/pull/11795)
and [#11813](https://github.com/agentydragon/ducktape/pull/11813). The importer job and its
scripts were removed by the cutover change. Current source points the recorder at the generated
CNPG `uri` Secret through `RECORDER_DB_URL`.

The cutover PR recorded that pgloader imported 29,806 rows without errors, schema version 53 was
present, and counts matched SQLite across the 13 history tables checked. The original SQLite
database remains on the Home Assistant config PVC.

## Remaining acceptance checks

This audit found no evidence that the post-merge checks have been completed. Confirm that:

- Home Assistant is ready.
- The History panel shows records from before the cutover.
- New recorder writes appear in PostgreSQL.

Keep the SQLite file and its VolSync snapshots until these checks pass. If a check fails, follow
the rollback noted in [#11813](https://github.com/agentydragon/ducktape/pull/11813). Delete this
plan after the checks are recorded as complete.
