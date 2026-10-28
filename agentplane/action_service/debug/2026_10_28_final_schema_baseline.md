# Action storage version 7 baseline verification

Version 7 retires the upgrade-only versions 1–5 SQL and Go version 6 conversion from the
migrator image. It creates the final typed schema on a fresh database or adopts a completed
version 6 database in place. Existing Goose history is retained. The adoption guard checks
that Goose versions 1–6 are applied, the sixteen expected tables and six authored indexes
exist, the typed cutover columns have their required types and nullability, and the retired
JSON and OAuth columns are absent. It refuses older, unmarked, and partial schemas. The
version 6 cutover was atomic, so its applied Goose record is the primary evidence that the
full historical conversion committed.

A temporary PostgreSQL 18 integration experiment ran the production versions 1–5 SQL and
Go version 6 migrator in one disposable database, and the new version 7 baseline in another.
It compared application columns (names, types, nullability, defaults), constraints, index
definitions, and sequence definitions. The catalogs matched. Applying version 7 to the real
version 6 fixture then left its application catalog unchanged. RBE invocation
[9a5087a5-21eb-4096-b6bf-83fc425234f1](https://app.buildbuddy.io/invocation/9a5087a5-21eb-4096-b6bf-83fc425234f1)
passed. The temporary old-code and duplicate-DDL fixture was removed after this proof.

Permanent PostgreSQL integration tests seed version 6 history and populated rows into the
parity-proven typed schema. They check adoption retains human descriptions and decision notes,
byte snapshots, prior Goose history and application table relfilenodes. They also check
repeated/concurrent adoption and rejection of older, partial, or unmarked schemas. The CLI
test covers concurrent and repeated fresh init
containers. The final baseline, CLI, Action store, and service integration targets passed on
[RBE invocation 13553929-2938-4641-890f-2942825298bf](https://app.buildbuddy.io/invocation/13553929-2938-4641-890f-2942825298bf).

The experiment proves schema parity for the code-defined version 6 schema. Before publication,
read-only staging checks confirmed version 6 applied, both new replicas ready with no restarts,
and matching pre/post row counts, non-converted-field fingerprints and normalized Action identities.
All 2,439 human decisions remain; the converted actor/evidence counts match their old sources.
See the [live version 6 cutover evidence](2026_10_28_native_storage_cutover.md). Version 7 is a
subsequent cleanup migration, not part of that observed rollout.

The version 7 adoption guard also passed against live staging in a read-only transaction,
with its creation DDL excluded. Goose remained at version 6; this validated the guard
without applying the cleanup migration.
