# Recorder cutover to Postgres

Moves Home Assistant's recorder from SQLite on the config PVC (`/config/home-assistant_v2.db`)
to the `home-assistant-db` CNPG Cluster through ordinary Flux changes. Home Assistant stays down
from the first merge to the cutover, so nothing is recorded in between and no history is lost.
Delete this plan once the cutover has run.

1. **Import** (`cluster/cdk8s/home_assistant/db.py`): one merge scales Home Assistant to 0 and adds
   the Cluster and the `recorder-import` Job. Its `prepare` container waits for Home Assistant's
   clean shutdown (the WAL file disappears), snapshots the SQLite file, logs each table's row count,
   then runs the same image against Postgres with a recorder-only config until the schema exists.
   Its `load` container copies the snapshot's rows into that schema with pgloader
   (`data only, truncate, reset sequences`); pgloader's summary prints rows read and imported per
   table.
2. **Verify:** read both containers' logs (agentplane `kubernetes_admin` `pods_log`) and compare
   the SQLite counts with pgloader's per-table imported counts; spot-check with `psql` against
   `home-assistant-db-rw` (for example the newest `states.last_updated_ts`). If the load failed or
   counts disagree, fix the Job in a follow-up change; Flux replaces it, and `truncate` makes the
   rerun safe. If foreign keys reject pgloader's load order, `disable triggers` needs a superuser,
   which the `homeassistant` owner is not.
3. **Cut over:** one change sets `recorder: db_url: !env_var RECORDER_DB_URL` (the Cluster's
   `uri`), restores `replicas: 1`, and removes the Job and `recorder-import/`. Check the History
   panel shows days before the cutover.

Fallback: skip the comparison and cut over anyway; the recorder then starts fresh in Postgres,
losing the ~10 days SQLite holds. `.storage/` (devices, entities, zones, users) stays on the PVC
either way, as does the old SQLite file until someone deletes it. To abandon the move instead,
restore `replicas: 1` without the recorder change.
