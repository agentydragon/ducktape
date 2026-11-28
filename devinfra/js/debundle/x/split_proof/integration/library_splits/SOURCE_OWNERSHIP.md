# Source-file ownership of the four full readable bundles

This audit uses the unchanged SHA-256 pins in [source_pins.json](source_pins.json)
and checks every ordinal and UTF-16 span against the [full blocker census](FULL_BLOCKER_CENSUS.md).
It compares that census's four readable `halves` requests with four new
`source_files` requests on the same bytes. The new requests assign each
single-file unit to its mapped source home and leave the remaining units in
entry. Nonentry owner priority follows first appearance in the bundle. Rust
uses that priority; strict LowerSplit explicitly requests sequential placement,
so its scheduling contract is stronger. The minified bundles, ladder
extractions and historical structural previews are outside this audit.

| Readable bundle  | Bundle bytes | Single-file units / input span bytes | Source-file homes | Generated glue | Fixture entry | Unmapped | Outside all units (bytes) |
| ---------------- | -----------: | -----------------------------------: | ----------------: | -------------: | ------------: | -------: | ------------------------: |
| Acorn 8.19.0     |      204,565 |                        446 / 203,627 |                25 |        1 / 405 |             — |        — |                       533 |
| lodash-es 4.18.1 |       97,358 |                         669 / 76,739 |               202 |        1 / 290 |             — |        — |                    20,329 |
| semver 7.8.6     |       78,583 |                          47 / 72,651 |                47 |     10 / 1,400 |        1 / 50 |   1 / 42 |                     4,440 |
| Zod 4.6.7        |      767,238 |                      1,048 / 716,800 |                90 |    23 / 38,216 |        1 / 92 |        — |                    12,130 |

The counts are disjoint within each row. No unit had anchors in multiple
original files. The classifier would keep such units in the mixed bucket.
Single-file means all known anchors in that unit agree; it does not
mean every byte has a mapping. Acorn's 25 source homes include five
upstream-generated data files (five units, 10,849 span bytes). They retain their
file attribution and are distinct from glue produced while bundling. Zod's
23 glue units include esbuild namespace shells and export tables; some map back
to export syntax, so counting every mapped unit as source implementation
would overstate recovery. The unmapped semver unit is its generated-looking
entry alias; the audit does not infer its origin from neighboring statements.

## Where the file names come from

- **All four bundles:** esbuild 0.28.2 emits an external map and build metafile
  in a second build. The builder checks that the JavaScript is byte-for-byte
  identical before writing the sidecars. The audit checks the historical pins,
  verifies the metafile's output size and input membership, and records both
  map and metafile evidence. Metafile `bytesInOutput` is an independent esbuild
  measure; it is neither a source-map span count nor an emitted Rust byte count.
- **Acorn:** npm ships `dist/acorn.mjs` without the authoring map. Rollup 4.60.1
  and `@rollup/plugin-buble` 1.0.3 from the upstream [8.19.0 release lock](https://github.com/acornjs/acorn/blob/0e0ece7cc7202624d4aca531be4b515b0114b5d0/package-lock.json)
  rebuild the exact published distribution after removal of the appended map
  URL. The recorded map composes esbuild anchors with that release map.
  It leaves 1,011 first-hop mapped span bytes without an upstream anchor.
- **lodash and semver:** direct map attribution to the published
  `lodash-es/*.js` and semver package JavaScript input files, including
  individual semver CommonJS factory bodies. The lodash count describes its
  published ES module layout; it does not infer boundaries in the separate
  monolithic lodash build.
- **Zod:** all 95 mapped npm JavaScript inputs were reproduced byte for byte
  from their shipped `src/**/*.ts` counterparts with TypeScript 5.9.3 and
  the ES2020 compiler settings in the test. Ninety homes survive as single-file
  implementation units; the remaining inputs are represented only by glue.
  This establishes file identity, not a TypeScript column-level map.

Source-map coverage counts bytes from an anchor inside a unit to the next
segment, line end or unit end, whichever comes first. The algorithm does not
carry ownership across a line, a generated-only segment or a unit boundary.
Span bytes count the whole parsed unit, including its unmapped interior.
Comments and whitespace between units are accounted for separately. A
CommonJS wrapper can have one file home without making its bundler scaffolding
authored code.

## Requested placement and observed output

For halves, the table gives the requested `m0.mjs` and `m1.mjs` unit / input
span-byte counts. For source files the first table and the ledger give the
counts by requested file. The Rust selector export verified the same input hash
and owner for every ordinal in all eight requests. Output ownership below is
inferred only when a top-level AST matches uniquely on both sides, independent
of its pathname and printing. Import/export rows, rewrites and nonunique
matches are left unknown. All observed matches landed in the requested owner.
Artifact bytes count the printed JS files, including imports and exports.

| Bundle | Halves requested (left; right) | Layout       | Requested owners | Rust files / emitted bytes | Original units / bytes located in Rust output | Strict files / emitted bytes |
| ------ | ------------------------------ | ------------ | ---------------: | -------------------------: | --------------------------------------------: | ---------------------------: |
| Acorn  | 223 / 111,645; 224 / 92,387    | Halves       |                2 |                2 / 219,786 |                                 446 / 203,627 |                        0 / 0 |
| Acorn  |                                | Source files |               26 |               26 / 234,384 |                                 445 / 203,333 |                        0 / 0 |
| lodash | 335 / 30,496; 335 / 46,533     | Halves       |                2 |                 2 / 84,408 |                                  669 / 76,739 |                        0 / 0 |
| lodash |                                | Source files |              203 |              203 / 244,069 |                                  669 / 76,739 |                        0 / 0 |
| semver | 29 / 25,709; 30 / 48,434       | Halves       |                2 |                 2 / 89,748 |                                   57 / 64,189 |                        0 / 0 |
| semver |                                | Source files |               48 |                      0 / 0 |                                             — |                        0 / 0 |
| Zod    | 536 / 534,208; 536 / 220,900   | Halves       |                2 |                      0 / 0 |                                             — |                        0 / 0 |
| Zod    |                                | Source files |               91 |                      0 / 0 |                                             — |                        0 / 0 |

All five Rust outputs matched the original public API smoke probe in Node.
Rust refused semver's source-file layout with a module-quotient initialization
cycle and both Zod layouts with an atomic-group conflict. All eight strict
requests refused the requested `all-outcomes` guarantee with
`no_constructed_rule`, before producing a deployment. A refusal has no actual
owner or behavior observation. Rust smoke results are finite observations;
they do not establish the strict proof scope.

Grouping exposes what the halves counts hide. Among units with known source
and output homes, compare correctly co-located pairs against all known output
pairs (different-source pairs in one output count against the score). Also
compare the same numerator against all pairs from the same source file in the
input (unknown matches reduce this lower bound).

| Rust output         | Correctly together / known output pairs | Correctly together / same-source input pairs |
| ------------------- | --------------------------------------: | -------------------------------------------: |
| Acorn halves        |                         10,062 / 49,506 |                              10,062 / 10,638 |
| Acorn source files  |                         10,597 / 10,597 |                              10,597 / 10,638 |
| lodash halves       |                         2,106 / 111,556 |                                2,106 / 2,106 |
| lodash source files |                           2,106 / 2,106 |                                2,106 / 2,106 |
| semver halves       |                                 0 / 522 |                 — (one unit per source home) |

Lodash halves keep every source file intact, yet co-locate 109,450 pairs from
different files. Acorn's source-file layout leaves one authored unit unlocated
by the AST matcher; that is an evidence gap, not evidence of a dropped unit.

## Read or regenerate the evidence

The [compressed ledger](results/source-ownership.json.gz) contains source
hashes, UTF-8 and UTF-16 ranges, per-unit input and original file attributions,
per-owner requested and actual counts, and emitted file bytes. Join the
existing census by library variant and source SHA-256; join requests by
`specSha256`. The `grouping.perFile` records give surviving members,
destinations, largest recovered group(s), unrelated members, residual members,
and unknown output owners. `rust.output.matches` and `strict.output.matches`
give the observed ordinal and file where available.

Run the audit at optimized settings, since the full Zod Rust graph can exceed
the child-process deadline in fastbuild. The target writes
`source-ownership.json.gz` as an undeclared test output:

```sh
p=//devinfra/js/debundle/x/split_proof/integration/library_splits
bbr test -c opt "$p:source_ownership_test" "$p:source_ownership_unit_test" \
  "$p:standalone_parser_dump_test" "$p:eslint_test" \
  --test_output=all --remote_build_event_upload=all
```

[Recorded replay on d6fa1cf3](https://app.buildbuddy.io/invocation/cceda8fa-d96f-4a00-8782-b990203e1db4).
To refresh the Acorn map, use the release commit above and its locked Rollup
and Bublé dependency closure with install scripts disabled. Run
`rollup -c acorn/rollup.config.mjs --sourcemap` from that checkout. Pass the
unmodified npm `package/dist/acorn.mjs`, rebuilt `acorn/dist/acorn.mjs`,
and rebuilt `acorn/dist/acorn.mjs.map` to
[`regenerate_acorn_map.mjs`](regenerate_acorn_map.mjs), followed by
`results/acorn-8.19.0-rollup-map.json.gz`. The helper refuses any rebuild
difference besides the map URL and stores the distribution hash alongside
the coordinates. The test checks that hash against esbuild's embedded
distribution input before composing the maps.
