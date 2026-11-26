# Kine compaction transaction failure

K3s exited on the same fatal Kine transaction commit in a VM test on November 18 and in five
MCP Actions cases sharing a control plane on November 26. Both retained logs show a slow SQLite
delete immediately before process exit. Neither records what canceled the transaction.

## Backend and placement

`test_vm` starts the pinned K3s binary as a child process of the BuildBuddy test action. K3s embeds Kine and serves its etcd-compatible API on the `kine.sock` Unix socket. No external datastore endpoint is passed, so Kine uses its default SQLite backend. K3s maps that database to `<data-dir>/server/db/state.db`; the test harness defaults `<data-dir>` to `/var/lib/k3s-env`.

The K3s process and SQLite file are on the test executor, outside the KubeVirt workload VM. `stack.py` places the database on a 256 MiB tmpfs only for Firecracker fixtures; `test_vm` uses KubeVirt, so its K3s database is on the executor filesystem. The stack code explains that the tmpfs option keeps control-plane commits off the bulk guest-disk write queue. This makes executor disk contention plausible, but the retained artifacts contain no disk latency or queue measurements to establish it as the cause.

The test binary is downloaded from the official K3s release as a Bazel `http_file`, pinned at SHA-256 `ef1df26b26276c0f6d962391c94e12f9362d1f1e30ec7a7926e9ddb84b0d66ce`. It reports `v1.34.12+k3s1`; that K3s source `go.mod` pins Kine `v0.16.5`. The separate `rancher/k3s` OCI image pin is not the binary source used by this test. K3s disables Kine automatic compaction (`--compact-interval=0s`), leaving compaction to the Kubernetes API server. Kine's `compact-timeout` default is five seconds; the test does not pass a timeout override.

## Mechanism

In Kine `v0.16.5`, `SQLLog.compact` makes a five-second timeout context and passes it to `BeginTx`, but passes the longer-lived Kine server context (`s.ctx`) to the transaction's revision reads, compact-revision write, and `DELETE`. `database/sql` binds the transaction lifetime to the context passed to `BeginTx`: when it is canceled, it rolls the transaction back. Because the SQL operation uses `s.ctx`, a slow `DELETE` can continue after that rollback. Kine then calls `MustCommit`; the transaction is already done, so `Commit` returns `sql.ErrTxDone`. `MustCommit` calls `logrus.Fatalf` on that error, terminating the K3s process. The Kubernetes API timeouts and refusal on `127.0.0.1:6443` follow from that process exit.

The retained log for [BuildBuddy invocation `ba5af734-eafe-57ff-89d8-badd5dbf16dc`](https://app.buildbuddy.io/invocation/ba5af734-eafe-57ff-89d8-badd5dbf16dc) shows the second batch's `DELETE` starting at `01:09:23.930Z` and taking `6.689852685s`; the next Kine line is `level=fatal msg="Transaction commit failed: sql: transaction has already been committed or rolled back"` at `01:09:30Z`. The API server logs `http: Handler timeout` and etcd-client `DeadlineExceeded` at `01:09:29.424Z` immediately before it. This timing and `ErrTxDone` fit the timeout/rollback path.

There is an unresolved counterexample in the same retained log: the preceding batch reports `COMPACT deleted 355 rows from 1000 revisions in 7.443793808s` and succeeds, despite Kine's five-second source default. Its SQL `DELETE` itself took `3.715204721s`. The test action did not retain Kine's effective timeout value or a transaction-cancellation event, so the successful batch prevents proving that the second batch's configured deadline expired at five seconds. `ErrTxDone` proves the transaction was already finalized before `MustCommit`; the timeout is the source-supported explanation, but the exact cancellation and timing discrepancy remain unresolved.

## Recurrence in the MCP Actions suite, November 26

On PR #20120, `test_actions` attached to a warm K3s baseline. Its `k3s-retained.log` shows
Kine starting compaction at 08:52:59 UTC. Controller lease requests timed out at 08:53:04.
At 08:53:06 the 7.016-second SQLite delete returned, the commit reported the same fatal error,
and K3s exited. History requests timed out, and teardown found the K3s process dead. The failed
transaction was in K3s's Kubernetes datastore. The PR changes the Sampler's PostgreSQL journal.
[Failed invocation](https://app.buildbuddy.io/invocation/6615419d-f82d-548a-bb24-e6c87459e623) ·
[Passing MCP run on its base](https://app.buildbuddy.io/invocation/8a26b653-61c0-58e8-abe4-2c174e73559b)

## Upstream mitigation, checked November 26

[Kine issue #790](https://github.com/k3s-io/kine/issues/790) records the fatal on embedded
SQLite. [Kine PR #806](https://github.com/k3s-io/kine/pull/806) propagated the transaction's
timeout context into the compaction reads, write and delete. A timeout in those operations
can then return before the commit. Confirmation that this resolves both observed failures
requires evidence from a binary containing the change.

The fixture pins K3s 1.34.12. The next stable patch, [1.34.13](https://github.com/k3s-io/k3s/blob/v1.34.13%2Bk3s1/go.mod),
still pins Kine 0.16.5. The November 25 [K3s 1.35.11 release candidate](https://github.com/k3s-io/k3s/blob/v1.35.11-rc1%2Bk3s1/go.mod)
pins [Kine 0.16.7](https://github.com/k3s-io/kine/blob/v0.16.7/pkg/logstructured/sqllog/sql.go#L252-L305),
which uses the timeout context throughout. The [1.37.3 candidate](https://github.com/k3s-io/k3s/blob/v1.37.3-rc1%2Bk3s1/go.mod)
also carries the change in [Kine 0.17.6](https://github.com/k3s-io/kine/blob/v0.17.6/pkg/logstructured/sqllog/sql.go#L252-L305).
The earlier stable 1.37.2 carried Kine 0.17.1, which lacks it. Check the bundled source when
choosing a candidate. A K3s 1.34.12 test build with the Kine 0.16.6 backport would isolate
the mitigation without a Kubernetes minor-version upgrade. Neither path has been tested here.

## Source references

- [Test K3s startup and data directory](../k3s/baseline.py): defaults to `/var/lib/k3s-env`, launches `k3s server`, and does not pass a datastore endpoint or compaction timeout.
- [Test stack storage placement](../deployment/stack.py): enables the database tmpfs only for Firecracker fixtures.
- [Bazel K3s binary artifact pin](../../../devinfra/bazel/artifacts.MODULE.bazel) and [artifact extension](../../../devinfra/artifact_pins.bzl): the official release `http_file` and its checksum.
- [K3s v1.34.12 SQLite path and endpoint configuration](https://github.com/k3s-io/k3s/blob/v1.34.12%2Bk3s1/pkg/etcd/etcd.go#L279-L281), [default endpoint flags](https://github.com/k3s-io/k3s/blob/v1.34.12%2Bk3s1/pkg/etcd/etcd.go#L1700-L1708), and [Kine dependency](https://github.com/k3s-io/k3s/blob/v1.34.12%2Bk3s1/go.mod#L109).
- [Kine v0.16.5 default SQLite endpoint](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/app/app.go#L38-L42), [five-second compaction timeout](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/app/app.go#L184-L190), [v0.16.5 compaction transaction](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/logstructured/sqllog/sql.go#L227-L285), [v0.17.1 still using the parent context](https://github.com/k3s-io/kine/blob/v0.17.1/pkg/logstructured/sqllog/sql.go#L227-L285), [v0.17.3 using the timeout context](https://github.com/k3s-io/kine/blob/v0.17.3/pkg/logstructured/sqllog/sql.go#L227-L285), [fatal commit handling](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/drivers/generic/tx.go#L42-L50), and [`database/sql.BeginTx` context semantics](https://pkg.go.dev/database/sql#DB.BeginTx).

## Separate earlier test_vm result

The `d35927a1` run (Actions run `44206100998`) was the known QEMU `iotlb_to_section` assertion from [#16057](https://github.com/agentydragon/ducktape/issues/16057), not the Kine fatal or the guest timer/RCU stall. Its trace recorded `failure: ... index=162 ... sections_nb=161`, and the artifact metadata says `stop_reason="assertion-observed"`. [BuildBuddy invocation](https://app.buildbuddy.io/invocation/58c1064c-6d79-546f-8607-f919244155ec).
