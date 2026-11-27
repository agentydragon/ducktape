# Ordinary CLI workload checkpoints

The baseline measurements use ordinary `//devinfra/js/debundle/x/split_proof:lowersplit`
at `d10843cd8d0fd27e7c84dd4a89204663bbbdf31c`. See the
[public placement ladder](COMPLEXITY_LADDER.md) for the earlier checkpoint,
and [the historical generated matrix](results/summary.md) for diagnostic
plans and Node comparisons. Structural acceptance and a matching Node smoke
run do not establish an ordinary CLI certificate.

## What is pinned

- [Library generation](prepare.mjs) uses esbuild 0.28.2 on Acorn 8.19.0,
  lodash-es 4.18.1, semver 7.8.6 and Zod 4.6.7. Each has a readable and a
  minified output. The current cohort runs the readable outputs to count each
  library once. The historical records include six variants and many generated
  cuts, blocks and random layouts. The cohort uses the existing, contiguous
  halves assignment; each of the four corresponding historical halves rows
  passed the structural checks and Node smoke. The ordinary request explicitly
  enables sequential evaluation in that same contiguous order. The cohort does not add random
  or entry-first layouts, many of which failed the structural checks.
- [Excalidraw](../excalidraw/README.md) pins v0.18.1 and publishes original
  chunk [hashes](../excalidraw/main_static_sha256.json). The full authored
  editor and main specs are distinct diagnostic replays. Scroll keeps the
  original editor bytes and a two-owner placement. The historical heading,
  geometry, and transform-handle subsets required earlier proof routes and
  browser contracts. They are not current constructor successes.
- The [debundle corpus](../../debundle_cases/README.md) has 207 authored
  entries in its saved report. The ordinary certification runner selects
  122 previously `agree-accept` rows. The dedicated catchall export fixture
  supplies a separate known-good compiled-constructor request.

## Current requests and outcomes

| Existing request family                                                                 | Certified | Refused, no output | Evidence                                                                                                                                                             |
| --------------------------------------------------------------------------------------- | --------: | -----------------: | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Full readable library halves, one per library                                           |       0/4 |                  4 | [Cohort](https://app.buildbuddy.io/invocation/a7d2a9ee-85c0-4497-bc62-d3306cbc5f73)                                                                                  |
| Additional full Acorn and semver requests (advance second; also Acorn's first function) |       0/3 |                  3 | [Library ladder](https://app.buildbuddy.io/invocation/7218de38-c37b-4034-a207-4c0b63036550)                                                                          |
| Excalidraw scroll pair on unchanged editor bytes                                        |       0/1 |                  1 | [Same focused run](https://app.buildbuddy.io/invocation/7218de38-c37b-4034-a207-4c0b63036550), [first refusal and projected slices](../excalidraw/scroll_refusal.md) |
| Debundle corpus ordinary adapter                                                        |     0/122 |                122 | [Corpus replay](https://app.buildbuddy.io/invocation/70bcd72d-a120-442f-a1ad-6652cc5b8ea9)                                                                           |
| Dedicated corpus catchall export request                                                |       1/1 |                  0 | [Same focused run](https://app.buildbuddy.io/invocation/7218de38-c37b-4034-a207-4c0b63036550): emitted, disk rechecked, Node matched                                 |

The unsafe read-before-initialization control, wrong-owner control, tampered
artifact, and projected Excalidraw examples are outside the denominators.
The extra Acorn and semver rows exclude the two halves requests already counted.

The 122 baseline corpus results measure the adapter as well as the CLI. It
serializes `privateEntryBody: true` and omits `sequential`; the latter
defaults to false. The singleton dispatcher requires `sequential: true`
before it even attempts source classification. Multi-chunk deployments
separately fail the singleton check. Rust's `import_order` is a priority,
whereas `sequential` fixes the module evaluation order. Adding that switch
to every corpus request would change the requested schedule in cases with
dependencies that determine evaluation order.

A [priority-order constructor trial](https://github.com/agentydragon/ducktape/pull/20608)
at `a3347e27a1f29749495e014b7d0154876e57d1c4` replayed the same 122 adapter
requests. It certified **1/122** and refused the other 121 without output
([six-shard run](https://app.buildbuddy.io/invocation/b07ada08-a3c3-4a11-bced-3c94fbb3c2ee)).
The successful request was `emission/catchall_exports/unclaimed`:
`residual.mjs` owns #0, `entry.mjs` owns the public export row #1, and the
original priority is `[residual.mjs]`. The separately tested export probe
still sees `foo` and `"x-y"`, both bound to the original value. This is an
unchanged corpus request, distinct from the dedicated one-row proof fixture
and from the Acorn excerpts.

An [additional source-only audit](https://app.buildbuddy.io/invocation/4ec09ce8-f03c-4a27-bc79-c5615352c584)
on the same #20608 head used the unchanged adapter for input and owner resolution.
A temporary test-runner probe then called the authenticated source decoder,
indexed admission, and the indexed local-export route's sequence and
ordered-const source checks. It recorded one row per request in
`priority-sources.jsonl` and did not invoke construction or emission:

| First source check that stops the local-export route | Rows |
| ---------------------------------------------------- | ---: |
| Linked empty-profile lowering                        |  115 |
| No standalone local export row                       |    2 |
| Ordered const body check                             |    4 |
| Passed these source checks                           |    1 |

Of the 115 lowering refusals, **90** first report `unsupported global console`.
Five first report `unsupported global Object`; three first report
`unsupported global WeakMap`. These are counts of first errors. They do
not estimate how many requests would pass after any one check changes.
The only row that passed these source checks is the catchall export request
that actually certified.

Do not broaden the priority-order route based only on the four body refusals.
The declarations-only case, `emission/catchall_member_rename/spec`, requires
checking the renamed binding in its specified owner. When the chunk already
exports the binding, the original public name and the readable name must be
live aliases, as specified in the [debundle spec](../../../../SPEC.md#readable-names-and-the-chunk-public-surface).
The adapter's stdout check does not establish those properties. The other three cases exercise
async calls or class initialization and are not const-only witnesses.
The [two no-export-row requests](https://app.buildbuddy.io/invocation/83093515-a58b-437b-a8ea-33f36d546372)
do not justify a metadata-only route. The aliased-factories case keeps every
unit in the entry. The eager class callback case moves #1–2 to `mod_b.mjs`
and #0 and #3 to `mod_a.mjs`, in that priority. The [unchanged replay](https://app.buildbuddy.io/invocation/56240d4d-61b7-4740-a8dc-93fea42a6144)
finds the cycle evaluates `mod_a.mjs`, then `mod_b.mjs`, then the entry.
The eager class heritage callback and its hoisted capture need a proof. Both cases remain in the unchanged
cohort denominator; only the latter is a split candidate.

The [Acorn projection test](https://github.com/agentydragon/ducktape/pull/20609)
grew from 0/3 at the baseline to 1/3 with initialized `var` and **2/3** with
the [order-preserving array rule](https://github.com/agentydragon/ducktape/pull/20612)
at `f929c5f529a4fcd0ca820fe264ec81027eb7ecf4`
([array replay](https://app.buildbuddy.io/invocation/fc3b18c9-5912-4126-acd9-316c5313a83d)).
The reordered pair and the unsafe eager read still refuse. A separate
four-unit projection keeps all original Acorn declarations #0–3 in
`m0.mjs`, in source order, with two observation exports in `m1.mjs`.
The same source projection
[refused on the baseline](https://app.buildbuddy.io/invocation/edf03107-1598-4607-8b03-2c422f7ba674)
and [certified at all outcomes on that array head](https://app.buildbuddy.io/invocation/8337de12-c574-435f-ab36-8bcbddebe67e).
Both files passed readback. Node matched both array lengths (344 and 634),
distinct identities, independent mutation and the observed Unicode string.
The added observation exports and the omitted parser code make this
a projection. It changes no full-bundle count.

## Acorn projections that call original exports

[The baseline replay](https://app.buildbuddy.io/invocation/a6f7025f-13ab-4667-bc88-58103479edc6)
and [the same replay on the dense-var array head](https://app.buildbuddy.io/invocation/96c5743a-2e03-4828-8106-33d1207e6d45)
refuse both requests without emitting files. Track them separately from the
full-bundle, three-projection, and four-declaration counts above.

| Actual public function                     | Retained original body ordinals | First observed boundary                                                                                                                                                               |
| ------------------------------------------ | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `isIdentifierStart` and `isIdentifierChar` | #0–3, #8–12                     | Source lowering refuses eager `new RegExp(...)` at original #8 (slice #4).                                                                                                            |
| `isNewLine`                                | #22                             | Source decodes; the inline constructor probe refuses the standalone export row. The separate local-export route requires const declarations, so it cannot admit the hoisted function. |

The projection uses the module-scope definitions, writes and references to
derive its lexical dependency closure. Complete declarations retain their
original text, relative order and halves owner `m0.mjs`. The original #446
export row in `m1.mjs` retains just the selected export specifiers. Node calls
the actual predicates in both unsplit inputs: ASCII, BMP and astral identifier
checks (including disabled astral mode), and newline detection across ASCII
and Unicode. No new bindings or observation exports are needed. These
comparisons check runnable source projections; neither run produced certified
output.

The slice retains both eager `RegExp` constructors at #8–9. The ambient
`String` in #11–12 is referenced during the BMP call path. Replacing the
regexp constructors with literals or exercising only calls that skip the BMP
branch would not remove the original initialization obligation. The smaller
full semver fixture already stops at the eager `Object.create` access in its
esbuild prelude. On [#20615](https://github.com/agentydragon/ducktape/pull/20615) at
`4b641fe17dde4ca902b56a88562d80d4c4b583e6`, the
[independent replay](https://app.buildbuddy.io/invocation/f083dd84-9a39-47c7-ba6f-ecfa49c4c0b5)
preserves the same `isNewLine` source hash
`add995a3f3dced274dbcaeb833465c1bc25e3b2d309042da5fd2d54ee0ffc494`
and the same owner assignment. That request goes from **0/1 to 1/1**:
the ordinary CLI returns `all-outcomes` through `indexed-exported-function`,
emits `m0.mjs` and `m1.mjs`, and both files pass disk readback. Node calls
through the emitted entry match all six observations. The inline-only probe
still declines the standalone export row; the new constructor handles it.
The identifier request remains **0/1** at the same eager `RegExp` boundary.
Only the comparison uses #20615; the workload replay can land independently.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:acorn_consumer --test_output=all
```

## First boundary for the four full library inputs

The cohort writes the original source hash, cut, exact spec, verdict and
separate source/indexed probes to the test artifacts. Its classification
probe (`singletonSourceShape`) uses the linked empty-profile lowering and
requires an import-free source. `indexedInlinePlan` additionally checks the
annotated source ranges against the actual bytes before trying one indexed
constructor. Both probes use opaque-capture annotations. Their results are
separate from the ordinary CLI verdict.

| Library | Units | First source unit refused | Decoder error               |
| ------- | ----: | ------------------------: | --------------------------- |
| Acorn   |   447 |                         8 | `unsupported global RegExp` |
| lodash  |   670 |                         0 | `unsupported global global` |
| semver  |    59 |                         0 | `unsupported global Object` |
| Zod     | 1,072 |                         0 | `unsupported global Object` |

All four stop before a constructor checks the proposed boundary.
The repeated `Object` diagnostic covers two of four complete library inputs,
but it does not establish that their two uses share a sufficient host contract
or that either could split once that lookup is modeled. The Excalidraw scroll
pair has the different eager `Uint8Array` boundary. Keep host and loader
behavior out of this round. Smaller public source projections can test ordinary
allocation, capture or constructor prerequisites without changing these
baseline requests.

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/integration/library_splits:workload_cohort" \
  "$p/debundle_cases:ordinary_certification_test" --test_output=all
```

## Replay on devel after the Acorn and lodash constructor changes

[The replay at `8dea4b95`](https://app.buildbuddy.io/invocation/240cdc1f-51bc-49eb-b030-dfee718683ae)
reproduced **0/4** for the pinned full-library halves and **1/2** for the
consumer projections. The four full sources stop at the same source
boundaries listed above. The original `isNewLine` placement certifies at
`all-outcomes`, passes disk readback, and matches the six Node observations.
The identifier projection still refuses at original unit #8. The separate
[Acorn line-scanning placements](ACORN_LINE_INFO.md) use a different
projection and are outside both counts.
