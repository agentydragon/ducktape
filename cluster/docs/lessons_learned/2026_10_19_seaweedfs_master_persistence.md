# SeaweedFS master persistence migration, 2026-10-19

Completed at 08:20 UTC: all three masters moved to independent 4 GiB
`local-path-ovh-ssd` claims, preserving Raft identity and allocated volume IDs.
Disk recovery, controlled leader failover, retained S3 reads and fresh writes
passed. Normal Flux, Helm, operator and RollingUpdate management was restored;
the disposable canary was removed. Configuration landed in
[#11074](https://github.com/agentydragon/ducktape/pull/11074).

The operator could not convert immutable claim templates in place, and a new
StatefulSet could not recreate later members while earlier Pods had incompatible
volumes. The migration held reconciliation, converted members individually with
the controller absent, and adopted the persistent Pods afterward.
[The disposable rehearsal passed](https://app.buildbuddy.io/invocation/51c19584-9230-41ec-9a8b-d184bc7b9df3);
temporary tooling remains in PR history (#11086/#11088), without landing on devel.

During preparation, the migration agent accidentally deleted the old StatefulSet
and master 1 through raw DELETE requests intended as dry-runs. Nonempty
DeleteOptions bodies omitted `dryRun`, overriding the query's `dryRun=All`.
For such requests, dry-run options belong in the body. Surviving masters retained
quorum; master 1 was restored before the controlled migration continued. Actual
request interruption duration was not measured.

A separate frozen-leader experiment elected a new Raft leader but timed out on
S3 writes because clients retained TCP connections to the stopped process.
Persistence and the passing Pod-deletion checks do not resolve that failure.
