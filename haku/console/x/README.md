# haku/console/x — experimental console surfaces

In-flux work that the console runs but does not yet promise. Nothing here has a stable API,
and the console must keep serving with any of it switched off.

## Matrix chat surface

`matrix_client.py` + `matrix_sync.py`: the console signs in as the `@haku` bot, long-polls
`/sync`, joins the operator's DM invite, and (today) echoes back. This is phase 0 of
<../../plans/matrix_chat_runtime.md> — the credential, the loop, the watermark, and the send
path, proven before an Agent SDK turn depends on any of them.

Wiring that necessarily lives outside this directory, because the stable modules own it:

- `MatrixConfig` and `Settings.matrix` in <../config.py> — absent config, or a config whose
  reflected bot password has not landed yet, means the loop does not start and the console
  does (R10.3b).
- `MatrixSyncState` in <../database_schema.py> and its Alembic revision, since migrations are
  one lineage for the whole database.

Only one replica syncs: the loop holds a Postgres advisory lock for its lifetime, because
`/sync` is a long poll and releasing between passes would let two replicas double-process a
batch.
