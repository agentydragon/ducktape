# Whole Lodash `_baseSortedUniq.js` with an unchanged provider

The existing [split-proof npm lock](../../pnpm-lock.yaml) supplies
`lodash-es@4.18.1`. The fixture checks the byte sizes and SHA-256 digests
of the three files it uses and verifies that the shipped package declares
`"type": "module"`.

Only the complete `_baseSortedUniq.js` (756 bytes) is submitted for
placement. It imports the default of the complete `eq.js` (797 bytes),
which has no imports. The unchanged public `sortedUniq.js` (511 bytes)
calls the target and is outside the split. The three files form the whole
dependency graph for this caller; the rest of Lodash is outside the test.

| Unit | Original statement                                       | Authored owner       |
| ---- | -------------------------------------------------------- | -------------------- |
| #0   | `import eq from './eq.js'`                               | `_baseSortedUniq.js` |
| #1   | hoisted `function baseSortedUniq(array, iteratee) { … }` | `sorted-uniq.mjs`    |
| #2   | executable `export default baseSortedUniq`               | `_baseSortedUniq.js` |

The [spec](lodash_sorted_uniq/split.json) requests `sorted-uniq.mjs` before
the entry. The moved function needs to capture the same provider default
cell when it calls `eq` to compare adjacent values. The entry must still
publish the original executable default expression. The authenticated
decoder recognizes the import as `eq.js: default → eq`, recognizes the
function, and retains all three ordinals and the separate default binding.

In Node, the shipped `sortedUniq` collapses repeated NaNs, normalizes signed
zero to positive zero, and collapses adjacent repetitions of the same
object without merging distinct equal-shaped objects. The result retains
the input object references and has a fresh outer array.

On [`devel` at `6fd48940`](https://app.buildbuddy.io/invocation/37d29fef-c902-4371-8705-d5f513289da2),
the ordinary CLI refuses at `all-outcomes` with `no_constructed_rule`:
`static provider: expected a raw .mjs source and a root-level entry route`.
The entry is already at the root; the first failing condition is its
shipped `.js` extension. No output is created. `--check-output` on a
directory of original package files gives the same refusal and leaves the
directory unchanged. It does not reach artifact readback.

The static-provider constructor also restricts the provider to `.mjs` and
both modules to primitive synchronous declarations; it excludes function and
default export syntax. A production extension needs to establish the module
format for both the actual source and deployment and preserve the hoisted
closure, the provider's exact live cell, and the entry's executable default
snapshot through staged readback. Admitting the extension by itself would
not supply those guarantees.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:lodash_sorted_uniq_test
```
