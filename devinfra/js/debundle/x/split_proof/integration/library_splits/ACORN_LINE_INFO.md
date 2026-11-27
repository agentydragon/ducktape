# Acorn line scanning across two owners

The pinned Acorn bundle (`e2478dfecff35578381298798b067b66c0424057a99c4c6ddddc6f6b2fb45eee`)
has a real cross-owner call candidate: `getLineInfo` (#38) calls
`nextLineBreak` (B, #23), which calls `isNewLine` (A, #22). The test
keeps the lexical dependency closure of the two original exports
`getLineInfo` and `isNewLine`. It also retains the eager
`Position.prototype.offset` assignment (#36) so the returned position
has the same interface. No ambient binding is free in the projection.

All three requests read the same source
(`c3e458d7a67d6a8b781b7d3ff8052bc135db59b36eeac620c470f9a01b2c83ab`).
Projected #0–5 map to original #22, #23, #35, #36, #38, and #446.
The five body statements are copied verbatim in order; the original
export row keeps only the two selected specifiers. No public names are added.
The three specs author new ownership boundaries on this projection. They do
not change the full bundle's existing halves request.

| Request | Nonentry owners and priority                | Original units in `entry.mjs` |
| ------- | ------------------------------------------- | ----------------------------- |
| A       | `newline.mjs`: #22                          | #23, #35, #36, #38, #446      |
| B       | `scanner.mjs`: #23                          | #22, #35, #36, #38, #446      |
| A+B     | `newline.mjs`: #22, then `scanner.mjs`: #23 | #35, #36, #38, #446           |

The ordinary CLI certified **0/3** at the
[baseline `d10843cd`](https://app.buildbuddy.io/invocation/9bec84a4-aff5-43c1-8f0e-4b68a60b8368),
**0/3** on the [morning stack `02d1c8fb`](https://app.buildbuddy.io/invocation/5acb3dfd-2739-424d-993c-fdd8453a9be3)
(which includes [#20613](https://github.com/agentydragon/ducktape/pull/20613)),
and **0/3** on [#20615 at `4b641fe1`](https://app.buildbuddy.io/invocation/59fd3544-ddef-47dc-ab95-3f1a36c9d731).
Each request used `--require all-outcomes` and returned `no_constructed_rule`
without creating its destination. The test uses the same authenticated source
bytes for all three ordinary requests. A separate empty-profile source probe
decodes the whole projection.

On the morning stack the `ordered-helper` rule rejects A and B with
`expected lexical seeds, a nullary return helper and one const call`;
it rejects A+B with `expected one authored helper module`. The
`indexed-exported-function` route in #20615 expects a one-parameter return
predicate for A and B, and one authored predicate module for A+B. These are
constructor admission boundaries. They do not change the existing full
library or declaration projection counts.

The Node control calls `getLineInfo` on CRLF, Unicode line separators, and
text containing an astral character, and checks `Position.offset` on the
returned objects. It checks that a caller-supplied `charCodeAt` sees its
receiver and receives indices `[0, 1, 2]` for one CRLF scan. The independently
wired modules match the full pinned Acorn API in all three placements. Wiring
only the scanner to an always-false predicate preserves both public names
and every direct `isNewLine` result, but changes the CRLF result from
line 2, column 0 to line 1, column 3. The handwritten layouts serve only as
Node controls. If the CLI certifies, the fixture rechecks the emitted files
from disk and repeats the same observations through the emitted entry.
No emitted artifact was available on the baseline revisions above.

With the ordered predicate extraction constructor, A certifies at
`all-outcomes` on the same projection and owners. The entry keeps all four
executable rows and both original export specifiers. The proof preserves the
hoisted heap and the captured cells of every unit, including the scanner's
read of the extracted predicate. It also preserves the complete public
address table, so fresh calls use the same cells after initialization.

A+B also certifies with one additional concrete, hoisted function in the
second owner. The original order remains #22, #23, #35, #36, #38, #446.
The entry imports the live `isNewLine` and `nextLineBreak` cells; the scanner
imports the same `isNewLine` cell from the first owner. A and A+B use the same
ordered execution and exact export proofs.

With the separate hoisted swap rule, B also certifies at `all-outcomes`.
The planner and the file graph both evaluate the scanner first: the scanner
imports the entry's already initialized `isNewLine`, while the entry imports
the scanner's `nextLineBreak`. The full ordinal schedule is
`[1, 0, 2, 3, 4, 5]`, including the unchanged export row in the entry. Both
real links start with equal heaps and cells. In particular, the scanner keeps
its capture of `isNewLine` at `(1, 0)`, and `getLineInfo` keeps its captures
of `nextLineBreak` at `(2, 0)` and `Position` at `(3, 0)`. The proof applies
at the same fuel, even zero, and the stage and disk checks verify the actual
files and their public live cells.

All three emitted layouts pass the same Node observations and disk readback.
The test rejects a changed export clause in B that binds the scanner to
`getLineInfo` instead of `isNewLine`. It also rejects a B request moving an
extra eager `getLineInfo` call into the scanner: the flat source executes, but
on the cycle the call reaches `Position` before its initialization. Neither
negative changes the tree it checks. The independent cycle test reproduces
the eager difference in CoreJS at fuel 128 and checks the changed capture.
The A and union controls still reject modified artifacts, and reversed union
priority still refuses.

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/integration/library_splits:acorn_line_info" "$p/integration/library_splits:acorn_hoisted_cycle_test" \
  "$p:corejs_indexed_hoisted_swap_axioms_test" --test_output=all
```
