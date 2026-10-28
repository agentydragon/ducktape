# Batched description queries

Measured 2026-10-27 on one Linux host (AMD Ryzen 9 9950X3D), using the
optimized CLI from the batch-inspection implementation. Production Rust sources
match commit `7c12b73369`; engine SHA-256:
`1079b936e8802f3d1e09d441c30bd4b2ca909c8f43c1b6d995e9f7aad762e7bc`.

The public synthetic program declares `seed = 1`, then 4,000 bindings
`valueN = seed + N`, and logs all values. Its tree spec leaves everything in
the entry. The generated graph has 4,002 nodes, 8,000 edges and 4,740,077 bytes.
The 12 queries select `seed`, then `value0`, `value300`, through `value3000`,
with `--limit 20 --format json` and proposals disabled. All 12 batch reports
equal the corresponding standalone JSON reports.

`perf stat -r 5 -e task-clock,cycles:u,instructions:u` measured the same
query sequence as 12 processes and as one batch process, with inputs staged
before measurement. Filesystem caches were warm. Each command wrote its JSON
report to a file. Variation below is what `perf stat` reports.

| Per 12-query sequence | Separate commands |        One batch |
| --------------------- | ----------------: | ---------------: |
| User task clock       | 486.50 ms ± 0.96% | 49.92 ms ± 4.08% |
| User instructions     |     3.760 billion |    0.406 billion |
| Elapsed               |  500.3 ms ± 1.32% | 87.7 ms ± 24.88% |

The batch used about 9.7 times less user CPU time on this workload. Elapsed
measurements for the short batch varied substantially. These results describe
this small synthetic graph; they do not measure the reported 690 MB corpus,
selector-heavy module queries, proposer latency, or peak memory.

Separate `perf_wrapper.sh` recordings repeated each sequence 25 times at
199 Hz with DWARF call stacks. Both recordings and all wrapper reports completed
without lost samples or postprocessing failures. The standalone profile spent
67.74% of sampled cycles in SHA-256, primarily engine verification; the batch
profile spent 57.03% there. Avoiding repeated compatibility verification is a
major contributor on this graph, alongside loading the graph once. Each batch
still checks the complete captured baseline before querying. No fingerprint
checks were weakened.

Raw counters, recordings, input files and shell drivers were retained locally
under `/tmp/debundle-inspection-profile-20261027/`. The graph SHA-256 was
`65a1c6693ee4cc9def1da43aabb2390dbfeaadc05485023ff00d067aea91ccce`.

## Reproduce the workload

Build through the repository devshell, then stage a fresh directory. `run`
requires an empty output directory.

```sh
direnv exec . bb run -c opt //devinfra/js/debundle:debundle -- --help
export QUERY_PROFILE_ROOT="$(mktemp -d)"
cp bazel-bin/devinfra/js/debundle/debundle "$QUERY_PROFILE_ROOT/debundle"
python3 - <<'PY'
import json, os
from pathlib import Path
p = Path(os.environ['QUERY_PROFILE_ROOT'])
(p / 'snapshot').mkdir()
(p / 'modules/main').mkdir(parents=True)
source = 'const seed = 1;\n'
source += ''.join(f'const value{i} = seed + {i};\n' for i in range(4000))
source += 'console.log(' + ', '.join(f'value{i}' for i in range(4000)) + ');\n'
(p / 'snapshot/main.js').write_text(source)
(p / 'js-files.txt').write_text('main.js\n')
(p / 'vendor_marks.yaml').write_text('vendor_marks: []\n')
(p / 'spec_config.yaml').write_text(
    'main_chunk_id: main\nmodule_roots:\n  main: main\n'
    'inputs:\n  root: snapshot\n  js_list_path: js-files.txt\n'
    'write_js_tree: true\nunassigned_mode:\n  main: {kind: inline_in_entry}\n')
ids = ['seed'] + [f'value{i * 300}' for i in range(11)]
(p / 'ids.json').write_text(json.dumps(ids) + '\n')
(p / 'ids.txt').write_text('\n'.join(ids) + '\n')
PY
"$QUERY_PROFILE_ROOT/debundle" run \
  --tree-config "$QUERY_PROFILE_ROOT/spec_config.yaml" \
  --tree-modules "$QUERY_PROFILE_ROOT/modules" \
  --tree-vendor-marks "$QUERY_PROFILE_ROOT/vendor_marks.yaml" \
  --tree-source-root "$QUERY_PROFILE_ROOT" \
  --out-root "$QUERY_PROFILE_ROOT/output"
```

For the separate sequence, loop over `ids.txt` and invoke `describe "$id"`;
for the batch, invoke `describe --batch "$QUERY_PROFILE_ROOT/ids.json"`.
Both use these explicit options:

```sh
--graph "$QUERY_PROFILE_ROOT/output/reports/tree/main/dependency_graph.json" \
--modules "$QUERY_PROFILE_ROOT/modules/main" \
--source-root "$QUERY_PROFILE_ROOT/snapshot" --limit 20 --format json
```

Measure each complete shell driver with the `perf stat` command above. For
stacks, repeat the driver 25 times and pass it to
`perf_wrapper.sh --output-dir <fresh-profile-dir> -- bash <driver>` as documented
in [Bazel integration](../docs/bazel_integration.md#profiling).
