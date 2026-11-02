# Bootstrap COSI event storage probe (2026-11-02)

The bootstrap event log stores each output chunk and terminal result as a separate persistent
`BootstrapEvents` resource. A synthetic probe of commit
[`eec17da53e83a24d2eeb923b161333407b002f84`](https://github.com/agentydragon/ducktape/commit/eec17da53e83a24d2eeb923b161333407b002f84)
found memory amplification during Runtime construction and replay, and substantial per-event write
cost on the test host. A suffix read also scans the entire history. These measurements motivate
comparing storage designs; they do not establish production output volumes or latency.

## Method

An uncommitted temporary Go probe called the production `cosistate.Open`, `bootstrap.OpenLog`,
`Log.AppendOutput`, `Log.AppendResult`, `Log.Since`, and `runtime.New` APIs. It registered the
production persistent resource types and alternated stdout and stderr. A fresh process loaded and
replayed each completed database. The probe did not launch a bootstrap process, and its synthetic
chunk sizes do not represent observed pipe-read distributions. It checked event sequence, output
bytes and terminal result. The temporary probe source and raw profiles were not added to the repo.

There was one sample per case. The host was Linux 6.18.55, AMD Ryzen 9 9950X3D, Go 1.26.8, with
data on `/tmp` ext4. Append time includes serialized output and terminal writes, but excludes
closing the backing store. Fresh-process replay includes COSI open, `OpenLog` and `Since(0)`,
excluding process startup; the OS page cache was not cleared. Heap peaks were sampled every 5 ms; RSS is the process high-water mark from
`/proc/self/status`. Database sizes are the file's logical size and allocated disk blocks after
close. Payload sizes are MiB; heap and RSS measurements are decimal MB.

## Results

| Synthetic output             | Output events + terminal | DB logical / allocated | Append time | Fresh-process load and full replay | Replay peak heap / RSS |
| ---------------------------- | -----------------------: | ---------------------: | ----------: | ---------------------------------: | ---------------------: |
| 1 MiB, 64 KiB line pattern   |                   16 + 1 |           2 / 1.44 MiB |     1.104 s |                            58.3 ms |        7.57 / 61.60 MB |
| 10 MiB, 64 KiB line pattern  |                  160 + 1 |         16 / 10.63 MiB |    10.358 s |                           116.4 ms |      52.14 / 210.57 MB |
| 100 MiB, 64 KiB line pattern |                1,600 + 1 |    112.58 / 103.57 MiB |   111.164 s |                           814.9 ms |   610.41 / 1,995.30 MB |
| 1 MiB, 1 KiB printable noise |                1,024 + 1 |           4 / 2.10 MiB |    36.296 s |                           209.1 ms |      13.10 / 105.80 MB |

The 64 KiB chunk size is the maximum of the production script read buffer, not a guarantee that
reads are that large. The 1 KiB case demonstrates sensitivity to event count as well as byte count;
the per-event rates also differ between these single runs, so they are not a controlled comparison
of storage devices or payload patterns.

For 100 MiB, `runtime.New` alone took 578.2 ms and reached 131.16 MB sampled peak heap and
557.13 MB peak RSS. The engine and embedded Host were not started. Its post-GC heap profile
attributed 98.40 MB to protobuf byte unmarshalling during COSI backing-store load. The replay
profile attributed 109.57 MB to protobuf cloning in `Log.Since` and 96.27 MB to decoding; writing
peaked at 180.17 MB heap and 628.17 MB RSS. These profile figures are in-use heap attribution,
not total allocations or complete memory accounting.

## Suffix reads and implications

`Log.Since` lists, validates, clones and sorts the complete event history before filtering the
requested suffix. Ten synthetic empty-suffix reads after the 1 MiB / 1,024-chunk write averaged
17.45 ms each. A one-event suffix after 100 MiB took 235.2 ms. This was a direct probe of the API,
not evidence of a production watcher repeatedly polling it. At the measured revision, the
production `Since` call found in [`bootstrapErrorForLog`](../bootstrap/host.go) reads the terminal
event to recover the result. [`OpenLog`](../bootstrap/log.go) independently scans the history.

COSI's current in-memory backing loads persisted output bytes as resources. Compare a disk-backed,
indexed append-only log, an alternative COSI backend, or a bounded/incremental resource model using
the same workloads and actual bootstrap output distributions. Evaluate writes, restart, tail reads,
memory and failure semantics together; eliminating the suffix scan alone does not address retained
output bytes. Preserve dense ordered events, terminal-result and interruption recovery, and the
fail-closed boundary for uncertain writes. Do not infer production impact or choose a size cap from
these synthetic samples alone.
