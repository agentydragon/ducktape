# What a split publication receipt checks

This audit records the routes and receipt fields at devel `6b3da2f148`. The
[import guarantees specification](import_guarantees.md) states the graph,
client and deployment boundaries independently of this historical snapshot.
The owned-package caller defect
([#20657](https://github.com/agentydragon/ducktape/pull/20657#issuecomment-7230274306))
is tracked separately. The ordinary CLI selects these routes in
[Certified.lean](../EsmSplit/LowerSplit/Certified.lean).

At that revision a success reports a constructor `scope`, `strategy: compiled-constructor`,
`guarantee: all-outcomes`, and `every_ending_matches: true`. The guarantee
selects the CoreJS theorem about all modeled outcomes, worlds and fuel.
The receipt has no separate field for the deployment realization or for a
caller observation contract. It also does not say whether it was issued for
a fresh publication or a verification of an existing tree.

Since this audit, [#20669](https://github.com/agentydragon/ducktape/pull/20669)
added `receipt.graph`, `receipt.client`, `receipt.deployment`, and
`receipt.operation` for the covered static-import routes. The
[import guarantees specification](import_guarantees.md#deployment-realization)
explains their checked-versus-assumed distinction. The route table below is
the original audit, not a complete description of the newer receipt fields.

| Route                                   | Authenticated source and modeled public observations                                                                                                                                                                                              | Deployment evidence and limits                                                                                                                                                                                                       |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `named-declarations`                    | Import-free authenticated source, exact original declaration selections, checked ordered initialization and live entry exports.                                                                                                                   | Generated `.mjs` only. No provider, caller, or enclosing deployment is included.                                                                                                                                                     |
| `literal-reorder`                       | Same source admission; the specific independent literal transposition preserves the run and exported cells.                                                                                                                                       | Same generated-only `.mjs` boundary.                                                                                                                                                                                                 |
| `indexed-local-exports`                 | Import-free primitive const body, original standalone export rows, exact public live cells and order (including the supported private-priority plan without extra entry exports).                                                                 | Same generated-only `.mjs` boundary.                                                                                                                                                                                                 |
| `ordered-helper`                        | Import-free raw source; keeps the authored helper, captures and original declaration identities. Ordered run and public cells are checked.                                                                                                        | Generated `.mjs` only; no external caller or ambient context checked.                                                                                                                                                                |
| `indexed-exported-function`             | Import-free raw source, original standalone exports, capture and cell identities; [proof](../EsmSplit/CoreJS/IndexedExportedFunction.lean) also covers modeled calls through public cells.                                                        | Generated `.mjs` only. The Acorn Node callers are regression observations.                                                                                                                                                           |
| `indexed-hoisted-swap`                  | Import-free raw source, exact adjacent declaration swap in the constructed graph, real links and public cell agreement.                                                                                                                           | Generated `.mjs` only. The checked import cycle is internal to the split.                                                                                                                                                            |
| `indexed-default-snapshot`              | Import-free raw source; the separate default cell snapshots the original closure at its authored ordinal. The linked run and entry cells are checked.                                                                                             | Allows generated `.js` as well as `.mjs`. It checks ESM syntax, but **does not establish Node's interpretation of `.js`** at source or destination. The Lodash consumer regression supplies its own package manifest and dependency. |
| `shared-static-provider`                | One raw `.mjs` importer and one closed relative `.mjs` provider, resolved from the real source directory. The provider runs once and first; the proof checks its live cells, importer aliases, and the ordered consumer suffix and entry exports. | Generated `.mjs` plus the provider at its checked relative route. Provider bytes are compared exactly; no declared caller, package, origin, or external loader is authenticated.                                                     |
| Diagnostic and test-only artifact tools | Diagnostics write plan and move reports, without using the production publication path. The private checked-artifact runner validates fixture artifacts against its own theorem.                                                                  | Neither issues a production CLI receipt.                                                                                                                                                                                             |

The ordinary source decoder reparses raw source (or validates a source-backed
snapshot on the routes that admit one). The common
[publication gate](../EsmSplit/SplitFiles.lean) checks the expected file set,
reparses generated files, compares their normalized ESTree, and passes the
actual parsed files to the constructor's exact CoreJS readback check.
Fixed provider files are byte-checked. Fresh publication verifies the stage
before renaming it; `--check-output` reconstructs from the supplied input and
checks the existing tree without updating it. It is not a historical record
of which invocation created the directory.

## Reproduced format gap

The original pinned `lodash-es/isLength.js` and its unchanged
[placement](../integration/library_splits/lodash_is_length/split.json)
emit byte-identical `isLength.js` and `limits.mjs` in two fresh destination
trees. The only difference is the parent `package.json` outside each output
tree. Both fresh publications and both `--check-output` calls report the
same success. Node imports and calls the default successfully under
`"type": "module"`; under `"type": "commonjs"` it refuses to load
`isLength.js` with a syntax error. The original callable returns `true` for
the input `3`.

[Original uncached replay](https://app.buildbuddy.io/invocation/c181210b-e07e-4fd6-9dde-71c3d27cca76).
The [manual reproduction](../integration/library_splits/default_snapshot_format_audit.mjs)
now also checks the newer receipt fields and operation distinction; the Node
loadability difference remains. Change the CommonJS assertion when a reviewed
admission or deployment change addresses that difference.

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/integration/library_splits:default_snapshot_format_audit" \
  --nocache_test_results --test_output=all
```

## Contract

The [import guarantees specification](import_guarantees.md) states the
approved graph and client boundaries and the separate deployment
obligation. This audit is evidence for that contract; it does not implement
importability checks or change a CLI or receipt schema.
