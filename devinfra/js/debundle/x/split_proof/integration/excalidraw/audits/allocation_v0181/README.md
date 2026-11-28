# Excalidraw v0.18.1 allocation audit

Pinned release: [Excalidraw v0.18.1](https://github.com/excalidraw/excalidraw/tree/v0.18.1).
Measurement base: [ducktape devel b394c69](https://github.com/agentydragon/ducktape/commit/b394c691eb63582cc35cbfa13a24a86a4a3e09c8).
[Current Rust build and artifact reports](https://app.buildbuddy.io/invocation/59d1e6ca-f314-4bc0-a50e-ed7578306232).

| Input chunk       | Original rows | Import/export metadata | Executable | Known owner | Correct named | Known in residual | Unknown in residual | Known wrong named | Unknown in named |
| ----------------- | ------------: | ---------------------: | ---------: | ----------: | ------------: | ----------------: | ------------------: | ----------------: | ---------------: |
| Editor            |         1,787 |                      6 |      1,781 |         501 |           403 |                98 |               1,280 |                 0 |                0 |
| Main              |         1,628 |                      8 |      1,620 |          71 |            71 |                 0 |               1,549 |                 0 |                0 |
| Other 77 chunks   |         1,897 |                    216 |      1,681 |           0 |             0 |                 0 |               1,681 |                 0 |                0 |
| **All 79 chunks** |     **5,312** |                **230** |  **5,082** |     **572** |       **474** |            **98** |           **4,510** |             **0** |            **0** |

A source unit is one original Rust graph row, identified within its pinned chunk by statement ordinal. Executable rows include initializers, declarations and effect-only statements, including setup or prototype writes where the selector evidence assigns them an owner. Static imports and standalone exports are metadata: editor has five imports and one standalone export; main has eight imports. The full row denominator preserves the numbering used by the historical reports.

“Known” is bounded by the authored Rust source selectors and their v0.18.1 owner audits. The full-context placement report supplies the original owner and binding matches even for units that did not move in the canonical output. The audit matches original unit IDs into the emitted Rust groups and checks each named group for missing or extra members against those assignments. “Unknown” means there is no owner assigned by this evidence; it includes the other chunks without such a report. The production bundle disables source maps. The ledger does not give a separate count of ambiguous upstream origins or a denominator for the unmapped part of any upstream file. A zero in the wrong/extra columns is a result within this evidence boundary.

- [owners.tsv](owners.tsv) contains 51 editor and 24 main owners. It tracks correctly placed members, retained members, members sent to a different named owner, and extra units sent into each output. Across known editor bags, **31/51** are complete, **6/51** are partial, and **14/51** have no correctly placed named output; main is **24/24** complete. Completeness here means that every known member is together in the right named module and that the output has no extra rows. Use the member counts in each row as its denominator.
- [chunks.tsv](chunks.tsv) accounts for every input chunk, including units with unknown owner, and records whether each corresponding entry file is byte-identical. “Retained” describes placement: the unit stays outside named modules, whether in the entry or a residual group. Editor retains 1,378 executable rows in its entry. Main puts 1,549 executable rows and eight metadata rows in 19 residual groups; its entry has no original rows. All 474 correctly placed units have a reported readable binding name that differs from the original binding (one compared binding per unit, zero unchanged). This compares the Rust public/export names and allows for aliases in the emitted text. Names of retained rows were not audited for spelling changes.
- [destinations.tsv](destinations.tsv) lists each emitted named module, each generated residual group and each entry, with its source-row counts and actual file size. The 19 main residual groups are identified by their graph contents and Rust residual flag. The graph, rather than the binding inventory, counts effects and metadata that do not declare a member of a group.

| Input chunk              | Original JS bytes | Named JS bytes | Entry JS bytes | Residual group JS bytes | Unjoined JS bytes | Total emitted JS bytes |
| ------------------------ | ----------------: | -------------: | -------------: | ----------------------: | ----------------: | ---------------------: |
| Editor                   |           517,159 |        245,176 |        584,830 |                       0 |                 0 |                830,006 |
| Main                     |           802,981 |        472,069 |          2,303 |               1,013,851 |                 0 |              1,488,223 |
| All chunks and bootstrap |         4,937,119 |        717,245 |      4,532,853 |               1,013,851 |               140 |              6,264,089 |

The manifest SHA-256 checks cover all physical JS files in this output. File sizes include formatting and embedded import/export glue; they are not source-byte coverage. The 2,303-byte main entry and the 140-byte bootstrap are wholly generated and listed separately from their original code. None of the 77 emitted entries has exactly the original chunk bytes. The two vendor wrapper entries have no physical file in this manifest; their six metadata rows are still in the source count.

## Reconcile the snapshots

| Placement or view                                       | Editor                                                                 | Main                                                                                                                                                       |
| ------------------------------------------------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Historical canonical Rust snapshot](../../recovery.md) | 272 of 1,787 total rows in 35 named modules                            | [1,628 of 1,628 outside entry in 43 modules](../../README.md#building-and-replaying-the-authored-excalidraw-specs), including 19 generated residual groups |
| [Historical joint deployment selection](../../joint.md) | 365 executable units across 45 owner bags                              | 1 executable unit, `strokeEllipseWithRotation`                                                                                                             |
| Historical selection joined to current canonical output | 283 of the 365 correctly named; 82 retained                            | 1 of 1 correctly named                                                                                                                                     |
| Current canonical Rust output, full owner join          | 403 correctly named in 37 named modules; 98 other known units retained | 71 correctly named in 24 named modules; 1,557 total rows outside named modules, all in residual groups                                                     |

The 272 figure describes the older canonical output. The 365 figure describes a different selected deployment; it is neither an increment over 272 nor the full editor owner denominator. The current canonical output has 131 more named editor units and two more named modules than the older canonical count. The old aggregate alone does not establish which individual units changed. The checked-in selection does establish that overlap: joining its binding selectors to the pinned source ordinals gives the exact 283/82 partition, with the corresponding columns in both TSVs. The historical [316/317 owner audits](../owner_clusters/README.md) record outcomes for their particular placements and owner trials. For example, their 317 snapshot classified the 1,787 editor rows as 317 placed, 1 known residual, 129 in refused additions, and 1,340 unattributed. Those counts describe a separate trial, so they do not add to the current ledger.

## Reproduce and interpret

From this checkout, build the producer targets at the measurement SHA and point the audit at the downloaded Bazel artifact directory (the path below is the usual `bbr` output path):

```sh
m=//devinfra/js/debundle/excalidraw
bbr build --remote_build_event_upload=all \
  "$m:spec_graphs" "$m:canonical_deployment_placements" "$m:canonical_summary" "$m:debundle" "$m:bundle"
a=devinfra/js/debundle/x/split_proof/integration/excalidraw/audits/allocation_v0181
python3 "$a/account.py" bb-out/bazel-out/k8-fastbuild/bin/devinfra/js/debundle/excalidraw \
  devinfra/js/debundle/excalidraw/deployment_smoke_subset.json "$a"
```

The join uses the Rust graph and final module reports, the canonical deployment placement report (the full-context selector solve used to prepare the historical subset), the pinned raw bundle, and the Rust output manifest. It checks original chunk hashes for editor and main, ordinal continuity in every chunk, all historical selectors, original unit membership in named artifacts, and physical file hashes.

## Supported authored replay

The [replay at b394c69](https://app.buildbuddy.io/invocation/59d1e6ca-f314-4bc0-a50e-ed7578306232) passed the full authored test, diagnostic census, spec converter, and both scroll tests. The upstream harness change in [#20683](https://github.com/agentydragon/ducktape/pull/20683) replaced the obsolete forced path with read-only `--diagnostic --plan --move-report` and separate ordinary `--require all-outcomes` requests. The replay used the original bytes and unchanged selectors. It checks that diagnostics leave the JS output path absent.

| Plan                         | Rows classified (executable) | Concrete lowering | Opaque fallback | Lowering refused | Structural order conflicts | Private cross-owner uses |
| ---------------------------- | ---------------------------: | ----------------: | --------------: | ---------------: | -------------------------: | -----------------------: |
| Editor, full                 |                        1,781 |             1,705 |              68 |                8 |                     60,295 |                      208 |
| Main, full                   |                        1,620 |             1,566 |              37 |               17 |                    130,667 |                      968 |
| Editor, historical selection |                          365 |               364 |               1 |                0 |                     45,120 |                      138 |
| Main, historical selection   |                            1 |                 1 |               0 |                0 |                      1,300 |                        1 |

[replay.tsv](replay.tsv) breaks the lowering result down by requested destination, including entry and generated residual groups. It is generated by passing `--replay-outputs` with the test outputs directory containing `editor/owners.json` and `main/owners.json`. In the full editor plan, the named `data/blob` bag has two refusals; the entry has six refusals and all 68 opaque rows. In the full main plan, `components/app` has one refusal; generated residual groups contain the other 16 refusals and all 37 opaque rows. The selected editor table classifies only the selected 365 rows, while its order check includes all retained rows. Concrete lowering does not establish that the owner can move; order and constructor checks apply to the whole request.

For both full and selected diagnostics the move report stops at the eager call in editor #5 or main #8, before checking semantic move pairs. The order counts are the separate structural check. The ordinary CLI refuses the full editor and main requests (`no_constructed_rule`) and the historical joint request (no multi-chunk constructor). Individual selected owners were not ordinary CLI requests. Across these three requests there were zero published or certified files, so there was no disk check of an emitted split or browser exercise. The [strict placement report](../../strict_fidelity.md) describes the link and deployment obligations. The historical browser result applies to its own selected tree. This audit adds no host assumptions.

For example, `scene/scroll` is 3/3 against the known Rust owner bag. The [strict original-chunk probe](https://github.com/agentydragon/ducktape/pull/20701) still refuses publication, so those three rows are Rust placement coverage only.
