# Contract-audit preparation reuse

Measured 2026-10-28 on Linux x86-64. Baseline: `540bc64f8f82576662c217b94eecd18f69f25b0d`.
Both binaries use `-c opt --@rules_rust//:extra_rustc_flag=-Cdebuginfo=1`.
The changed binary reuses input preparation within an audit and hashes the
running engine once per process. Analysis, assignment, lowering and final
reconstruction acceptance still run independently for each contract.

## Results

Each case runs a baseline plus twelve independent removals. `perf stat -r 5`
reports the following mean elapsed times and task clocks:

| Public synthetic input                                                                                             | Before elapsed | After elapsed | Speedup | Before / after task clock |
| ------------------------------------------------------------------------------------------------------------------ | -------------- | ------------- | ------- | ------------------------- |
| Preparation-heavy: eight 1,500-function pass-through providers, one materialized constant (1,146,266 source bytes) | 1.409 s        | 0.098 s       | 14.4x   | 2,406 / 237 ms            |
| Analysis/lowering: 2,000 generated statements, ten claimed blocks, separate assertion provider                     | 1.794 s        | 0.691 s       | 2.6x    | 1,882 / 772 ms            |

The elapsed-time uncertainties reported by perf were respectively 0.34%, 3.38%,
0.73%, and 0.83%. A separate single `/usr/bin/time -v` run measured peak RSS at
106,344 -> 60,604 KiB for preparation-heavy input and 45,156 -> 44,832 KiB for
analysis/lowering input. These are observations on these fixtures, not a bound
on memory for large retained ASTs.

The baseline preparation-heavy `perf record` profile attributed 63.84% of
sampled cycles to SHA-256 compression. Code inspection found the executable was
rehashed by every input capture and verification; repeated parsing, resolution
and SWC hygiene work consumed additional CPU. Source, spec and package bytes,
missing inputs and tree membership continue to be checked on probe boundaries.
Only the immutable running engine's digest is memoized across those checks.

All five before and five after reports matched in each case after omitting
`input_provenance_sha256`, which includes the changed engine's identity. These
performance fixtures deliberately use twelve unused export assertions, so all
removals pass. The CLI differential regression separately exercises both passing
and failing removals against fresh `debundle run --dry-run` executions; the
pipeline test mutates captured inputs after preparation and requires an abort.

A private consumer's baseline gate rejection prevents a successful full audit comparison there.
These measurements establish the gain on the public synthetic inputs above.

## Reproduce

Build both revisions with the matching flags above, save their executables as
`/tmp/debundle-before` and `/tmp/debundle-after`, and stage the inputs before
profiling. From the repository devshell, generate the preparation-heavy fixture:

```sh
python3 - <<'PYGEN'
import json
from pathlib import Path
root = Path('/tmp/debundle-audit-reuse-benchmark')
source = root / 'source'
source.mkdir(parents=True, exist_ok=True)
(source / 'main.js').write_text('export const answer = 42;\n')
chunks = ['main.js']
for chunk in range(8):
    name = f'peer{chunk}.js'
    chunks.append(name)
    (source / name).write_text(''.join(
        f'export function spare{i}(value) {{ const result = value + {i}; return {{kind: "item", result}}; }}\n'
        for i in range(1500)))
(root / 'js-files.txt').write_text('\n'.join(chunks) + '\n')
(root / 'spec.yaml').write_text(json.dumps({
    'inputs': {'input_root': str(source), 'js_list_path': str(root / 'js-files.txt')},
    'unassigned_mode': {'main': {'kind': 'inline_in_entry'}},
    'materialize_logical_modules': {'prune_other_chunks': False},
    'chunk_export_purity': {'peer0': {'pure_exports': [f'spare{i}' for i in range(12)]}},
}, indent=2))
PYGEN
```

Generate the analysis/lowering fixture with the existing proposer generator:

```sh
python3 -m devinfra.js.debundle.perf.gen_synth_corpus \
  --out /tmp/debundle-audit-analysis-benchmark \
  --statements 2000 --seed 1 --claim-blocks 10
python3 - <<'PYGEN'
import json
from pathlib import Path
root = Path('/tmp/debundle-audit-analysis-benchmark')
(root / 'snapshot/static/peer.js').write_text(''.join(
    f'export function spare{i}() {{ return {i}; }}\n' for i in range(12)))
listing = root / 'extracted/js-files.txt'
listing.write_text(listing.read_text() + 'static/peer.js\n')
path = root / 'spec.json'
spec = json.loads(path.read_text())
spec['chunk_export_purity'] = {'static/peer': {'pure_exports': [f'spare{i}' for i in range(12)]}}
path.write_text(json.dumps(spec, indent=2) + '\n')
PYGEN
```

Run timing commands serially for each binary. For the second fixture substitute
its `spec.json` and `--policy-key static/peer`:

```sh
perf stat -r 5 -- /tmp/debundle-before spec audit-contracts \
  --spec /tmp/debundle-audit-reuse-benchmark/spec.yaml \
  --policy-key peer0 --max-probes 12 --format json > /tmp/audit-before.jsons
perf stat -r 5 -- /tmp/debundle-after spec audit-contracts \
  --spec /tmp/debundle-audit-reuse-benchmark/spec.yaml \
  --policy-key peer0 --max-probes 12 --format json > /tmp/audit-after.jsons
```

Use [perf_wrapper.sh](../perf_wrapper.sh) for a separate sampled profile. Keep
profiling and timing runs separate. The timing recipe's stdout contains five
consecutive reports; compare every report, allowing only engine provenance to
differ. Keep mixed accept/reject and stale-input controls in the Bazel tests.

Baseline binary SHA-256: `ab55222e03a170d5c3ecdd0e4cfb8c1f90d8350dd7d13871accd673fe086bcc2`.
Changed binary SHA-256: `b1343dca065bc6ce154e4da5318bc008612f91494f5b1bd5bb60b982afdfa0d6`.
Build invocations: [baseline](https://app.buildbuddy.io/invocation/166f2eff-5a88-4484-a240-d0fc55628e89),
[changed](https://app.buildbuddy.io/invocation/ef5a97bb-8db8-4edc-9971-ff61fc32c717).
