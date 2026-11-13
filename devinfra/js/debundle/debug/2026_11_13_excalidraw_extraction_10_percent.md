# Excalidraw extraction checkpoint

Input: the canonical Excalidraw 0.18.1 capture, grounded against upstream commit
`a2ec2889babf7d2295469c6d90ebe77fae57df84`. No captured source or gate assumptions
changed in this extraction batch.

## Measurement

The agreed coverage metric is named-module emitted bytes divided by all emitted
JavaScript bytes in the `main` and `editor` chunks. Count each emitted file once;
include their residual entry files in the denominator. This measures readable
output coverage, including generated imports and module headers.

| Measure                                    | Before this batch | After this batch |
| ------------------------------------------ | ----------------- | ---------------- |
| Named emitted bytes                        | 124,063           | 225,596          |
| All main/editor emitted bytes              | 2,209,440         | 2,224,659        |
| Named byte coverage                        | 5.62%             | 10.14%           |
| Named source units / all main/editor units | 164 / 3,415       | 236 / 3,415      |
| Named source unit coverage                 | 4.80%             | 6.91%            |
| Original module homes                      | 29                | 38               |

This denominator includes bundled dependencies inside those two chunks. Separate
vendor, locale, and font/WASM chunks are outside this metric. The 10.14% figure
does not describe coverage of the entire shipped application corpus or upstream
TypeScript source lines. Most main/editor code still resides in residual entries.

## Reproducing the count

Run the canonical `//devinfra/js/debundle/excalidraw:debundle` target and inspect
its generated tree:

1. For `main` and `editor`, read `reports/tree/<chunk>/modules.json`.
2. Sum each non-residual `final_module_contents[].file` under `app/<chunk>`.
3. Divide by the bytes of all emitted `.js` files under those two directories.
4. Count distinct `(chunk, source_unit_id)` pairs for the source-unit metric;
   obtain the denominator from each chunk's `graph_summary.json`.

The fresh combined pipeline accepted all 79 chunk translation reports, with
`gate_accepts` and `accepts` true and zero conflicts. Browser behavior and output
exports are additionally checked by `test_smoke` and `spec_output_test` in CI.

## Withheld claims

The gate still places `getGridPoint`, `getSizeFromPoints`, and `rescalePoints`
inside the existing 410-unit editor initialization group. The library atom is
inside a 262-unit main initialization group. Those claims remain residual. These
are constraints of the current analysis; the rejection alone does not establish
an actual runtime cycle. This batch did not reduce or change those constraints.
