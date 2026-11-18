# Kine compaction transaction failure

**Status:** The source defect is confirmed; this run’s transaction cancellation is not. The retained logs fit the timeout path, but do not record the cancellation or effective timeout, and the preceding successful 7.44-second batch is inconsistent with assuming a five-second deadline.

## Backend and placement

`test_vm` starts the pinned K3s binary as a child process of the BuildBuddy test action. K3s embeds Kine and serves its etcd-compatible API on the `kine.sock` Unix socket. No external datastore endpoint is passed, so Kine uses its default SQLite backend. K3s maps that database to `<data-dir>/server/db/state.db`; the test harness defaults `<data-dir>` to `/var/lib/k3s-env`.

The K3s process and SQLite file are on the test executor, outside the KubeVirt workload VM. `stack.py` places the database on a 256 MiB tmpfs only for Firecracker fixtures; `test_vm` uses KubeVirt, so its K3s database is on the executor filesystem. The stack code explains that the tmpfs option keeps control-plane commits off the bulk guest-disk write queue. This makes executor disk contention plausible, but the retained artifacts contain no disk latency or queue measurements to establish it as the cause.

The test binary is downloaded from the official K3s release as a Bazel `http_file`, pinned at SHA-256 `ef1df26b26276c0f6d962391c94e12f9362d1f1e30ec7a7926e9ddb84b0d66ce`. It reports `v1.34.12+k3s1`; that K3s source `go.mod` pins Kine `v0.16.5`. The separate `rancher/k3s` OCI image pin is not the binary source used by this test. K3s disables Kine automatic compaction (`--compact-interval=0s`), leaving compaction to the Kubernetes API server. Kine's `compact-timeout` default is five seconds; the test does not pass a timeout override.

## Mechanism

In Kine `v0.16.5`, `SQLLog.compact` makes a five-second timeout context and passes it to `BeginTx`, but passes the longer-lived Kine server context (`s.ctx`) to the transaction's revision reads, compact-revision write, and `DELETE`. `database/sql` binds the transaction lifetime to the context passed to `BeginTx`: when it is canceled, it rolls the transaction back. Because the SQL operation uses `s.ctx`, a slow `DELETE` can continue after that rollback. Kine then calls `MustCommit`; the transaction is already done, so `Commit` returns `sql.ErrTxDone`. `MustCommit` calls `logrus.Fatalf` on that error, terminating the K3s process. The Kubernetes API timeouts and refusal on `127.0.0.1:6443` follow from that process exit.

The retained log for [BuildBuddy invocation `ba5af734-eafe-57ff-89d8-badd5dbf16dc`](https://app.buildbuddy.io/invocation/ba5af734-eafe-57ff-89d8-badd5dbf16dc) shows the second batch's `DELETE` starting at `01:09:23.930Z` and taking `6.689852685s`; the next Kine line is `level=fatal msg="Transaction commit failed: sql: transaction has already been committed or rolled back"` at `01:09:30Z`. The API server logs `http: Handler timeout` and etcd-client `DeadlineExceeded` at `01:09:29.424Z` immediately before it. This timing and `ErrTxDone` fit the timeout/rollback path.

There is an unresolved counterexample in the same retained log: the preceding batch reports `COMPACT deleted 355 rows from 1000 revisions in 7.443793808s` and succeeds, despite Kine's five-second source default. Its SQL `DELETE` itself took `3.715204721s`. The test action did not retain Kine's effective timeout value or a transaction-cancellation event, so the successful batch prevents proving that the second batch's configured deadline expired at five seconds. `ErrTxDone` proves the transaction was already finalized before `MustCommit`; the timeout is the source-supported explanation, but the exact cancellation and timing discrepancy remain unresolved.

## Upstream status and workaround

Kine [issue #790](https://github.com/k3s-io/kine/issues/790) records the same fatal with embedded SQLite. Kine [PR #806](https://github.com/k3s-io/kine/pull/806) merged to `master` on 2026-10-26 and changes compaction's transaction operations to use the timeout context. The change is present in [Kine `v0.16.6`](https://github.com/k3s-io/kine/blob/v0.16.6/pkg/logstructured/sqllog/sql.go) (2026-11-06) and [Kine `v0.17.3`](https://github.com/k3s-io/kine/blob/v0.17.3/pkg/logstructured/sqllog/sql.go) (2026-10-27). PR #809 proposed the same propagation and was closed after maintainers noted the change was already in #806. This is a source-level mitigation; upstream has not documented it as a verified resolution of issue #790.

The newest K3s release is [`v1.37.2+k3s1`](https://github.com/k3s-io/k3s/releases/tag/v1.37.2%2Bk3s1), published 2026-11-03; it pins Kine `v0.17.1`. Its [`go.mod`](https://github.com/k3s-io/k3s/blob/v1.37.2%2Bk3s1/go.mod#L108) satisfies the version range `>=v0.16.6`, but Kine `v0.17.1`'s compaction source still passes `s.ctx` to the SQL operations. The latest K3s release and its latest RC therefore do not contain this fix. K3s `v1.34.13+k3s1` and `v1.35.10+k3s1` still pin Kine `v0.16.5`.

The proposed upstream path is to wait for a K3s release that carries Kine `v0.17.3+` or the `v0.16.6` backport. A test K3s binary built from the current `v1.34.12` source with Kine `v0.16.6` would provide the fix without the Kubernetes minor-version jump. A 256 MiB tmpfs for this K3s database could reduce sensitivity to executor disk contention, but it would only mask slow compaction and the captured evidence does not establish disk contention; it is not the root-cause fix.

## Source references

- [Test K3s startup and data directory](../k3s/baseline.py): defaults to `/var/lib/k3s-env`, launches `k3s server`, and does not pass a datastore endpoint or compaction timeout.
- [Test stack storage placement](../deployment/stack.py): enables the database tmpfs only for Firecracker fixtures.
- [Bazel K3s binary artifact pin](../../../devinfra/bazel/artifacts.MODULE.bazel) and [artifact extension](../../../devinfra/artifact_pins.bzl): the official release `http_file` and its checksum.
- [K3s v1.34.12 SQLite path and endpoint configuration](https://github.com/k3s-io/k3s/blob/v1.34.12%2Bk3s1/pkg/etcd/etcd.go#L279-L281), [default endpoint flags](https://github.com/k3s-io/k3s/blob/v1.34.12%2Bk3s1/pkg/etcd/etcd.go#L1700-L1708), and [Kine dependency](https://github.com/k3s-io/k3s/blob/v1.34.12%2Bk3s1/go.mod#L109).
- [Kine v0.16.5 default SQLite endpoint](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/app/app.go#L38-L42), [five-second compaction timeout](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/app/app.go#L184-L190), [v0.16.5 compaction transaction](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/logstructured/sqllog/sql.go#L227-L285), [v0.17.1 still using the parent context](https://github.com/k3s-io/kine/blob/v0.17.1/pkg/logstructured/sqllog/sql.go#L227-L285), [v0.17.3 using the timeout context](https://github.com/k3s-io/kine/blob/v0.17.3/pkg/logstructured/sqllog/sql.go#L227-L285), [fatal commit handling](https://github.com/k3s-io/kine/blob/v0.16.5/pkg/drivers/generic/tx.go#L42-L50), and [`database/sql.BeginTx` context semantics](https://pkg.go.dev/database/sql#DB.BeginTx).

## Separate earlier test_vm result

The `d35927a1` run (Actions run `44206100998`) was the known QEMU `iotlb_to_section` assertion from [#16057](https://github.com/agentydragon/ducktape/issues/16057), not the Kine fatal or the guest timer/RCU stall. Its trace recorded `failure: ... index=162 ... sections_nb=161`, and the artifact metadata says `stop_reason="assertion-observed"`. [BuildBuddy invocation](https://app.buildbuddy.io/invocation/58c1064c-6d79-546f-8607-f919244155ec).
