# Transcript batch publication measurements

The PostgreSQL-backed `test_file_batches` target retains `cProfile` artifacts for
`SnapshotIngestor._publish_version`, including its transaction commit. The synthetic
input repeats one short valid physical line, then appends one distinct valid record.
These measurements cover compact mapping publication on an RBE test database;
they do not establish throughput or latency on the archive's production HDD storage.

[Invocation `422e78ea-ac18-412b-ae36-6ca9365eaa89`](https://app.buildbuddy.io/invocation/422e78ea-ac18-412b-ae36-6ca9365eaa89):

| Capture                     | Staged mappings | Publication cumulative time |
| --------------------------- | --------------: | --------------------------: |
| Initial 50,000 records      |          50,000 |                     0.462 s |
| Same prefix plus one record |               1 |                     0.014 s |

The growing case asserts zero staging mappings after each of the first 100 batches
and one after the final batch. Prefix parsing, raw-byte validation, parent-record
reads, and durable progress updates still scale with the transcript length.
Publication and staging cleanup scale with the divergent suffix; checkpoints and
changed-parent reconstruction can still copy the full record mapping in one
transaction. The 500-record / approximately 4 MiB bound applies to parsing and
staging batches, not the final publication transaction or a single oversized line.

Reproduce and inspect the profiler artifacts:

```sh
bbr test //x/ai_session_archive:test_file_batches
bbapi artifact list <invocation-id> --kind test
bbapi artifact cat <invocation-id> test_file_batches/test.outputs/publication-50000.txt
bbapi artifact cat <invocation-id> test_file_batches/test.outputs/publication-growing-50001.txt
```

The real-Restic truncation/deletion target timed out at 60.4 s in
`9247deb5-fb51-484c-a395-4ead46e3f808`. Its container log records 25.4 s loading
Postgres, 0.9 s loading Ryuk, and 4.9 s starting Postgres. The diagnostic rerun
in `422e78ea-ac18-412b-ae36-6ca9365eaa89` passed at 57.7 s without a timeout
change. Its 40-second faulthandler stack was inside the Restic diff subprocess,
which subsequently completed. This identifies a tight existing integration-test
budget; it does not establish the failed run's exact final blocking operation.
