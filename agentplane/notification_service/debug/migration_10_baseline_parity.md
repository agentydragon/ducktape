# Migration 10 baseline parity

On 2026-10-28, a schema-only staging dump was read with:

```sh
direnv exec /home/agentydragon/code/ducktape kubectl -n agentplane-staging exec postgres-3 -c postgres -- pg_dump -U postgres -d notifications --schema-only --no-owner --no-privileges
```

The dump was restored only into the disposable PostgreSQL 18 container used by `//agentplane/notification_service/migrations/schema_parity_experiment:schema_parity_experiment_test`. RBE invocation `c109ae07-fae0-4661-9670-4f996bfbd84c` passed. It compared fresh migration 10 with the restored v9 schema across application tables, logical column definitions, defaults and identity properties, validated constraints, index definitions, and sequence definitions/ownership. It excluded only Alembic, Goose, and the cutover marker from schema comparison. Dropped-column catalog ordinals were not compared.

The same test seeded applied Goose versions 0–9, a complete cutover marker, retained Alembic history, and rows in `github_ci_event` and `github_commit_event` sharing sequence ID 901. Migration 10 then adopted the database. The post-adoption application catalog matched fresh v10; application table relfilenodes, seeded rows, and Goose history 0–9 were unchanged; Goose recorded version 10 and migration 10 dropped the cutover marker. The matching sequence ID retained distinct `check_run` and `status` events.

The staging query was read-only. The experiment used a schema-only dump and synthetic rows in disposable databases; it did not write to staging or compare staging row contents.
