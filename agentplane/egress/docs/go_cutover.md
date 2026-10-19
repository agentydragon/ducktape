# Go egress cutover acceptance

Testing and staging completed the Go egress cutover on 2026-10-18 PDT.

- [#10816](https://github.com/agentydragon/ducktape/pull/10816) published the Go proxy and history
  migrator and moved the k3s fixture to those binaries.
- [#11076](https://github.com/agentydragon/ducktape/pull/11076) added Flux image policies and pin markers.
- [#11077](https://github.com/agentydragon/ducktape/pull/11077) switched testing.
- [#11079](https://github.com/agentydragon/ducktape/pull/11079) supplied proxy settings through environment
  variables; testing acceptance was repeated after its rollout.
- [#11078](https://github.com/agentydragon/ducktape/pull/11078) switched staging and removed the temporary
  Python deployment selection and obsolete image automation.

Both environments passed the following checks through disposable Sandboxes after rollout:
Kubernetes returned the probe's own ServiceAccount identity, authenticated Actions catalog and rules
API reads returned HTTP 200, and an unallowed destination returned HTTP 403 with
`X-Agentplane-Egress: denied; reason=no-rule`. Decision records confirmed credential substitution on
the allowed requests. The probes were deleted afterward; no model turn was exercised.

Both databases reached `egress_goose_version` 2, and a row selected before each cutover remained
present afterward. Staging's two Go replicas had zero restarts, both migration init containers
exited successfully, and the running proxy and migrator digests matched the Flux-selected published
images. Flux was Ready and the old Python proxy Pods were gone.

This completed the gate for deleting the old Alembic runner, ORM schema, migration files and handoff
fixtures. The landed Goose SQL and database version records remain intact; future schema changes
are new Goose migrations.
