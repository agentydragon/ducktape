# `match-selector` fastbuild versus optimized profile

## Finding

A `spec match-selector` probe over a 7.14 MB downstream chunk takes 19.80
seconds with target-config `fastbuild` binaries but 3.86 seconds with `-c opt`.
Both runs find the same unique target and peak near 1.55 GB RSS.

The earlier 19.5-second result was therefore not a production-pipeline
baseline. The pipeline's debundler attribute uses `cfg = "exec"`; direct
`bb run //devinfra/js/debundle:debundle` instead builds the runnable target in
the requested target configuration, which defaults to fastbuild. Always record
the compilation mode with selector timings.

The profile still exposes a real representation bug: lowering the
`STMT_LIST_BODY` carrier leaves two unreferenced AST variables with full
1,190,984-node domains. This inflates the request and memory footprint, but it
does not make OR-Tools slow: the saved request takes 0.02 seconds in the
optimized sidecar.

## Reproduction

Build and download both optimized binaries:

```bash
bazelisk build -c opt --remote_download_outputs=all \
  //devinfra/js/debundle:debundle \
  //devinfra/js/debundle/solver_backends/ortools_cpsat:selector_cpsat_solver
```

Then point the debundler at the sidecar, enable request and summary artifacts,
and time the probe:

```bash
export DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SOLVER=/path/to/selector_cpsat_solver
export DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_SUMMARY_JSON_DIR=/tmp/selector-summary
export DUCKTAPE_DEBUNDLE_ORTOOLS_CPSAT_REQUEST_PROTO_DIR=/tmp/selector-summary
/usr/bin/time -f 'elapsed=%e user=%U sys=%S maxrss_kb=%M' \
  /path/to/debundle spec match-selector \
  --source-file /path/to/7mb-primary-chunk.js \
  --match 'function decodeEntry(bytes) { let offset = 0, label, children; STMT_LIST_BODY; return [label, children]; }' \
  --no-slack \
  --format json
```

Rebuild with `-c fastbuild --remote_download_outputs=all` for the comparison.

## Measurements

| Phase                         |    Fastbuild |    Optimized |
| ----------------------------- | -----------: | -----------: |
| whole probe                   |       19.80s |        3.86s |
| fact-domain construction      |        7.18s |        1.41s |
| atom lowering                 |        2.10s |        0.25s |
| allowed-tuple simplification  |        1.10s |        0.11s |
| variables and targets         |        0.44s |        0.13s |
| complete model construction   |       10.85s |        1.91s |
| saved request through sidecar |        0.12s |        0.02s |
| maximum RSS                   | 1,560,108 KB | 1,546,124 KB |

The fastbuild whole run consumed 19.05 seconds of user CPU and 0.54 seconds of
system CPU. It was CPU-bound rather than blocked on I/O or the sidecar.

The compiled problem was identical in both modes:

| Measure                                |           Value |
| -------------------------------------- | --------------: |
| extracted facts                        |       3,131,689 |
| AST nodes in the full domain           |       1,190,984 |
| selector variables                     |              25 |
| selector atoms                         |              62 |
| compiled variables                     |              27 |
| variables with 1,190,984-value domains |               2 |
| variables with 903-value domains       |               2 |
| singleton variables                    |              23 |
| allowed-table rows                     |               2 |
| serialized request size                | 5,194,536 bytes |
| total serialized domain values         |       2,383,797 |

The optimized sidecar replay was measured by feeding the saved protobuf to
`selector_cpsat_solver` on stdin. This isolates protobuf decoding, model
construction, presolve, solving, response encoding, and process startup from the
Rust frontend.

## What the frontend does

The probe parses and hashes the complete JavaScript AST, extracts 3.13 million
tagged facts, and then clones or indexes them into relation sets, parent/child
maps, literal/name indexes, a value dictionary, and sparse variable domains.
This involves millions of tree/hash operations, string clones, comparisons,
sorts, and deduplications; it is not equivalent to allocating a few flat arrays.
Fastbuild magnifies that CPU work because Rust dependencies are not optimized.

## Remaining work

1. In native source-match lowering, avoid allocating node variables for the
   skipped expression-statement and identifier nodes that carry list holes.
2. Reject or prune unreferenced variables before backend serialization.
3. Add a regression that asserts no full AST domain remains for this selector
   and records serialized request size and RSS separately from elapsed time.
4. Measure the complete downstream pipeline using its actual execution-config
   debundler before prioritizing broader fact-store/index reuse.
