# Whole Lodash `isLength.js` module

The existing [split-proof npm lock](../../pnpm-lock.yaml) pins `lodash-es@4.18.1`
and verifies the npm tarball integrity
`sha512-J8xewKD/Gk22OZbhpOVSwcs60zhd95ESDwezOFuA3/099925PdHJ7OFHNTGtajL3AlZkykD32HykiMo+BIBI8A==`.
The test checks the size and SHA-256 of the complete shipped `isLength.js`
(800 bytes) and `isArrayLike.js` (828 bytes). It submits the package's original
`isLength.js` to `lowersplit` by path. No source is copied, edited, or bundled.

| Original unit | Shipped statement                | Intended owner | Misplaced export |
| ------------- | -------------------------------- | -------------- | ---------------- |
| #0            | `var MAX_SAFE_INTEGER = …`       | `limits.mjs`   | `limits.mjs`     |
| #1            | `function isLength(value) { … }` | `isLength.js`  | `isLength.js`    |
| #2            | `export default isLength`        | `isLength.js`  | `limits.mjs`     |

Both specs select every unit once. The [intended spec](lodash_is_length/split.json)
puts `limits.mjs` first so the bound is initialized before the entry publishes
its default. The function must continue to read that binding on calls. The
[negative spec](lodash_is_length/misplaced_export.json) moves the executable
default publication out of the entry.

On the [combined baseline `3a22b105`](https://app.buildbuddy.io/invocation/02850362-5f67-463b-b9b7-7fa48dbbe063),
both placements refused with `no_constructed_rule` at `all-outcomes` and
created no output. The decoder recognized all three units and the default export.
The declaration constructor requires simple declaration rows; the
exported-function constructor requires standalone local export metadata.
The negative pins refusal and the absence of output, even though the baseline
does not get far enough to report a separate export ownership error.

The original `isArrayLike.js` imports `./isLength.js` and calls it with
`value.length`. Node returns true at `9007199254740991`, false at
`9007199254740992`, and reads the getter once in each case. The caller and
its other dependencies are unchanged and outside the split request. This is
one complete ESM module from a multi-file package, not a whole-package split.

The shipped module never reassigns `isLength`, so the runtime control uses
two separate small modules. Both start with `candidate = 1` and expose
`replace(2)`. Through their namespaces, `export default candidate` still
returns `1` after the update: it evaluated the expression and initialized
the default cell at that statement. `export { candidate as default }`
returns `2` because it aliases the live local binding. The two forms are
not interchangeable; neither control replaces any shipped source.

The indexed default snapshot constructor now publishes the intended split
at `all-outcomes`. It preserves the original captured bound and adds the
implicit default cell to the planner footprint for the existing executable
row #2. That row reads the function during evaluation and initializes the
separate public cell; its original ESTree and ordinal survive disk readback.
The misplaced export still refuses and leaves any existing output intact.

The test loads the emitted split through unchanged copies of the original
package consumer and configuration. A separate executable fixture assigns a
new function after the snapshot. The split still exports the old function.
Either converting the snapshot to a live alias or moving the same expression
after the reassignment changes the Node result and is refused on disk reread.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:lodash_is_length_test
```
