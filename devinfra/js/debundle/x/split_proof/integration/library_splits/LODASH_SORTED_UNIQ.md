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

## Static package format

For Node's standard file loading rules, the shipped `package.json` with
`"type": "module"` is the package format fact for all three original
`.js` files. The fixture checks that their real paths are direct children
of the same real directory as that manifest. Keeping all three paths at
the root of a replacement package with the exact same manifest preserves
their file format, including for the untouched caller. An extra generated
module does not change that scope when it is a sibling in the same root.

The production [deployment manifest](../../EsmSplit/LowerSplit/Input.lean)
declares source routes and placements, with no way to declare a Node package
root, its fixed manifest, or the caller's placement in that root. The
[staging checker](../../EsmSplit/SplitFiles.lean) checks the generated
JavaScript as ESM syntax and compares listed fixed files by bytes; that
parse does not establish the loader's interpretation of a `.js` file.

The test exercises the existing artifact checker directly on the original
three-file graph. It supplies the package manifest, provider, and caller
as fixed bytes. It passes the artifact check before and after renaming that
whole root, and a static Node import calls the unchanged caller at the new
location. The checker refuses a changed or missing `type`, a missing
manifest, and a moved entry path. Node's own syntax check rejects the
untouched caller under explicit `"type": "commonjs"`. With
the manifest omitted from both the expected and actual file sets, the
artifact checker still accepts the same JavaScript. This last case
demonstrates the missing premise; it does not predict how a Node version
would classify a file in an undeclared package.

The bounded [artifact hook](../../EsmSplit/OwnedNodePackage.lean) accepts an
explicit canonical source root and three direct routes (entry, provider,
caller). Its caller is responsible for selecting these source assets and
for controlling the existing destination parent and scratch directory;
paths and contents must stay stable during the operation. The source root
is read-only to the hook. It captures the regular root `package.json` and
requires an explicit `"type": "module"`; it does not look for an enclosing
package or follow a manifest or module symlink. The generated entry keeps
its original route, and generated helpers stay at the root.

Fresh publication requires an absent destination. The shared gate checks
the exact file set and the captured manifest, provider, and caller bytes
before promotion; it rereads the same paths after renaming the whole root.
The reread mode uses the same comparison and never edits the supplied tree.
The proof callback gets the reparsed generated JavaScript and the captured
provider AST, after its bytes have been checked. The manifest and untouched
caller remain fixed assets. The fixture exercises the hook on the original
graph; it does not certify the proposed split.

An externally managed package root would require evidence for the actual
destination's enclosing package and a check that staging cannot change that
scope. The hook refuses that case. The production CLI still has no package
declaration, and the gate remains closed until the callable proof and
artifact hook are wired together.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:lodash_sorted_uniq_test
```
