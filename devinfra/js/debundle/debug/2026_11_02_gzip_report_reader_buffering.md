# Buffer decoded gzip report reads

`read_json_gzip` buffered compressed file input, but passed `MultiGzDecoder` directly to `serde_json::from_reader`. Serde's small `Read` requests therefore reached the decoder without a buffer at the decoded-byte boundary. `BufReader` around the decoder coalesces those requests while retaining `MultiGzDecoder`'s complete-stream, trailer, concatenated-member, and trailing-garbage behavior.

The existing `report_io_test` controls cover a valid report, truncated and checksum-corrupt streams, trailing garbage, concatenated gzip members containing a second JSON document, and invalid JSON. No fixture or test logic changed.

## Validation

The tested source was origin/devel `5f52f164b5aa240031767c595abadea3c5846122` plus the source patch applied by bbr as `9d031a8fcd7c511f2e59413e524eaa6a633cf546e103538c40ca3d98cf734580` (only `report_io.rs`). Invocation `1961d4f4-c3bb-47ab-9375-87cd8eadfd7e` ran:

```sh
nix develop -c bbr test //devinfra/js/debundle:report_io_test //devinfra/js/debundle/e2e:propose_commutations_cli_test //devinfra/js/debundle/e2e:propose_commutations_budget_test
```

All three targets passed with the default RBE and remote cache. The report I/O target took 0.8s, the CLI target 4.8s, and the budget target passed its unchanged 30s assertion (BuildBuddy target time 116.0s; test harness time 111.27s). The command invocation took 412.811s including runner setup and remote execution. The successful budget assertion establishes that this source meets the existing fixture contract; it is not an A/B measurement or a savings percentage.

The recurring CI failures motivating this change were on different source heads: cce290d4 had command duration 41.346s in `dfa2d8d9-fa46-5444-b93a-f9378081dc56`; 8ebd1878 had 35.913953412s in `c8a9da59-9cc6-51e6-aaa4-05941280b200`. Those runs are failure evidence, not matched performance controls for this devel change.
