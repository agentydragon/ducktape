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
No emitted artifact was available on the measured revisions.

The next bounded prerequisite is A alone on this unchanged source: preserve
the hoisted predicate's exact cell as the deferred scanner callee while
keeping the other functions, `Position` allocation, and prototype write in
the entry. A+B needs that same edge across the two extracted owners. B
alone also changes declaration order and produces a cycle through the entry;
success on A would not settle either of those extra checks.

The evidence PR can land independently. The comparison runs use the
already-published fork revisions above and require no merge.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:acorn_line_info --test_output=all
```
