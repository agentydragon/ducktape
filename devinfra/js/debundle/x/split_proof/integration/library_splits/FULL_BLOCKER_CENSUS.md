# Full library blocker census

[Machine-readable snapshot](results/full-blocker-census.json.gz), generated at
`d535b64e433b00100bde86154ce9e08eb2d59c39` by the [uncached census and lint run](https://app.buildbuddy.io/invocation/d93fa791-f13c-4270-8043-7e3ec2256554).
It uses the pinned `prepare.mjs` package entrypoints and includes readable and
minified Acorn, lodash, semver, and bundled Zod. The artifact carries byte sizes,
source and spec SHA-256s, exact selectors and priorities, original unit origins,
and half-open ESTree UTF-16 coordinates. Minified coordinates are offsets, not
line numbers. The separate Zod v3 installed graph is not this bundled workload.

| Source | Readable bytes / units | Minified bytes / units | Concrete first refusals by unit, readable / minified |
| ------ | ---------------------: | ---------------------: | ---------------------------------------------------: |
| Acorn  |          204,565 / 447 |          122,054 / 447 |                                                9 / 9 |
| lodash |           97,358 / 670 |           29,903 / 670 |                                              47 / 47 |
| semver |            78,583 / 59 |            29,296 / 59 |                                                6 / 6 |
| Zod    |        767,238 / 1,072 |        458,183 / 1,072 |                                              35 / 36 |

All eight halves and the three additional Acorn/semver ladder placements
refused through ordinary `lowersplit --require all-outcomes` and left no
output. The original bundles passed their Node smoke tests; there was nothing
to reread or run as split output. No projection, isolated probe, or archival
structural/Node result contributes an acceptance.

## How to read the evidence

- `placements` records the ordinary verdict, two independent decoder and
  constructor probes, and cross-owner reference and requested-order data.
  There are no static crossing writes or direct top-level inversions of
  definition-before-use among these requests, although the three ladder
  requests change the ordinal schedule. Reads through callees, class
  initialization, hidden mutation, and host effects need separate proof.
- `concreteLowering.unit_details` reports separately lowered units in their
  original lexical context, with opaque fallback and the first concrete error
  of each unit. All errors had an opaque fallback at this layer. That fallback
  does not prove the constructor can move, invoke, or faithfully reread it.
  Matching candidate spans for an unsupported global are not precise
  error locations if there is more than one reference of that name.
- `spans` records static occurrences, including multiple independent offsets
  within a top-level unit. It distinguishes ambient references from resolved
  locals and omits the `typeof` guard case from direct ambient references.
  Static presence is not a refusal, and a successful unit probe does not show
  that every nested body executes. `examples` records a small reproducer for
  each class and the result of independently lowering it; schedule or
  readability examples specify the control to apply. A successful isolated
  probe leaves whole-program support unknown.
- `archiveRows` retains all 2,892 historical observations, including
  `AssignsHome`, `OrderOk`, export ownership, and historical Node differences.
  It does not replay those placements. The archived Zod cuts describe 268
  units versus 1,072 now (762 historical rows across both variants). Equal
  unit counts for the other archived rows still do not authenticate old bytes.
  Neither archive supplies current ordinary verdicts.

## Work in placement-sized bundles

The census groups complete candidate feature sets by current request, so a
feature seen first or often does not receive credit for finishing a placement.
The counts below are the number of unresolved placements in each largest
intersection, not predicted acceptances. `verifiedFinishCount` is null until
the full original requests pass construction, staged readback and smoke after a
candidate change. No single-class fix is shown to finish any placement.

| Candidate intersection | Placements | First work to test together                                                                                                                                                                                                                                                   |
| ---------------------- | ---------: | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Readable Acorn         |          3 | Add a narrow, identity-checked intrinsic boundary for `RegExp`, `Object`, `Array`, and iterator symbols. Keep labeled control a separate residual check. Re-run the authored advance requests as well as halves.                                                              |
| Readable semver        |          2 | Treat the esbuild CommonJS bootstrap as executable setup with explicit state and captured cells; test intrinsic object operations and the sealed export boundary together.                                                                                                    |
| Both Zod bundles       |          2 | Compare stage admission under an explicit intrinsic profile for collections, errors, and symbols. Inventory Promise and async, dynamic compilation, and optional chains separately; test the selected runtime paths before calling them prerequisites.                        |
| Each remaining variant |          1 | For lodash, separate root/Node/browser lookup, dynamic function construction, timers and sparse arrays. For the remaining Acorn and semver minified rows, check readability and source mapping against the exact narrower names and offsets before assuming matching support. |

For ownership and order controls, start with a write to a binding owned by a
different module, a lexical read requested before initialization, and an export
list placed outside the entry. For constructor admission, use an initializer
and update in distinct owners. The JSON contains these examples together with
the standalone host/language probes and links every counted result to its
distinct full-source request.

```sh
p=//devinfra/js/debundle/x/split_proof/integration/library_splits
bbr test --nocache_test_results --test_output=all "$p:full_blocker_census"
gzip -dc devinfra/js/debundle/x/split_proof/integration/library_splits/results/full-blocker-census.json.gz |
  jq '.placements[] | {variant,id,specSha256,observed}'
```

The test writes JSON and gzip to its undeclared outputs. To recover the gzip on a
runner that only returns test logs, pass `--test_env=CENSUS_PRINT_ARCHIVE=1`,
then strip the `census-archive:` prefixes and base64-decode the concatenated lines.
