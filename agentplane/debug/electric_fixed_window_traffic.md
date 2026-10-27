# Electric traffic with a fixed older Thread window

Measured on 2026-10-27 at `085fed7ac5723ef9c139669d6072eea56d30ece2`:
[passing RBE invocation](https://app.buildbuddy.io/invocation/6550d4f8-9725-4c0c-8f95-6f14dac0cb0f).
The source is `app/test_electric_window_traffic.py`; the fixture runs Electric 1.8.1 with
`fast_file` storage against the real Go History Service's PostgreSQL fold.

```sh
bbr test //agentplane/app:test_electric_window_traffic \
  //agentplane/history_service/testing:electric_service --test_output=errors
bbapi artifact download 6550d4f8-9725-4c0c-8f95-6f14dac0cb0f electric-window-traffic.json
```

## Workload and accounting

The reader selects six completed older items, each with 2,048 bytes of text, from twelve initial
items. It opens one whole-thread entity shape and one text-field chunk shape with
`log=changes_only` and `replica=full`, then loads the selected rows and bodies through subsets.
It keeps those same shapes and selection, advancing their live offsets as each stage adds completed items and a new
active item with eight separately committed 2,048-byte appends. Each stage follows both logs to
the final active revision and samples memory after catching up; the consumer is not held open
continuously during the writes. The six selected item revisions stay unchanged.

The probe reads Electric HTTP long-poll response bodies directly with `Accept-Encoding: identity`.
The captured responses had no content encoding. Byte totals below exclude initial shape opens
and subsets, HTTP headers, framing and TLS. They include JSON/protocol overhead. Entity totals
also include view-state updates, which the UI needs even when reading an old window; those
changes are counted separately. Chunk changes all belong to unselected items.

`electric-window-traffic.json` contains the workload, per-response byte totals, offsets, change
counts, Docker `memory_stats` and Linux `ps` process RSS. Every raw response body is a sibling
undeclared output; container logs are also retained. These artifacts separate service accounting
from the test process, PostgreSQL and browser rendering.

## Observed traffic

| Newly completed items | Stored entity rows | Entity response bytes | View-state changes | Unselected item changes | Chunk response bytes | Unselected chunk changes |
| --------------------- | ------------------ | --------------------- | ------------------ | ----------------------- | -------------------- | ------------------------ |
| 32                    | 48                 | 64,052                | 24                 | 43                      | 101,340              | 40                       |
| 128                   | 177                | 152,987               | 26                 | 140                     | 344,690              | 136                      |
| 512                   | 690                | 505,549               | 32                 | 525                     | 1,319,202            | 520                      |

Neither shape delivered a change to a selected item. The live responses totaled **2,487,820
bytes**: 722,588 entity bytes and 1,765,232 chunk bytes. All 696 chunk changes were outside the
selected window. This quantifies the existing E5 mismatch: subset loading does not filter the
whole-thread live log. It does not show that all entity traffic is waste, since 82 view-state
updates were included.

## Observed memory

RSS is the Electric `beam.smp` process's Linux `ps` value. Container usage, anonymous memory
and file accounting are Docker's cgroup counters, sampled separately. They are not additive
with RSS. File accounting is reported directly; the probe makes no estimate of reclaimability
or unique resident bytes.

| Checkpoint             | BEAM RSS, MiB | Cgroup usage, MiB | Cgroup anon, MiB | Cgroup file, MiB |
| ---------------------- | ------------- | ----------------- | ---------------- | ---------------- |
| Before opening shapes  | 305.88        | 250.70            | 194.14           | 49.70            |
| Selected window loaded | 304.44        | 248.52            | 191.77           | 49.85            |
| 48 entity rows         | 301.74        | 245.69            | 188.71           | 50.05            |
| 177 entity rows        | 298.49        | 244.82            | 187.32           | 50.56            |
| 690 entity rows        | 293.18        | 241.64            | 182.18           | 52.47            |

This short run observed falling process RSS and container usage while file accounting grew.
Startup/garbage-collection effects can dominate a workload this small. These samples do not
establish a memory bound or rule out growth over longer histories.

## Remaining acceptance work

- A much longer history/update workload, including post-compaction behavior and shape lifecycle,
  is still needed for the server-memory gate. Sampling after catching up does not test a stalled
  downstream reader, replication backpressure or peak residency during an initial snapshot.
- This is a direct Electric fixture measurement, not staging browser traffic. It does not cover
  the published client's SSE/long-poll choices, app proxy, compression, concurrent readers or
  reconnect/restart behavior. The E5 off-window transfer requirement remains unmet by the
  whole-thread live log.
- Electric availability and horizontal scale remain unproven. D6 browser-cache retention remains
  the accepted deferred desire; this probe does not reopen it.
