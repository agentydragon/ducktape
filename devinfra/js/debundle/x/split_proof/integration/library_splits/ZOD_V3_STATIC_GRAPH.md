# Zod v3 static locale split

This fixture uses the complete published `v3/locales/en.js` from `zod@4.6.7`,
pinned in the [split-proof npm lock](../../pnpm-lock.yaml). It checks the
source SHA-256
`51a6dcc5d4bd52a64da127c012c6c963395c1d9a343ad75d4a8eca662fc8b205`.
The CLI receives that file byte-for-byte as shipped, with no flattening or
rewriting. The other nine original modules are fixed.

The [authored placement](zod_v3_static_graph/split.json) moves rows `#0` and
`#1` (the two static imports) and `#2` (the private `const errorMap`
implementation) to the sibling `v3/locales/en-error-map.mjs`. Row `#3`,
`export default errorMap`, stays in `v3/locales/en.js`. The reference tree
adds a named export to the moved declaration and a matching import to the
locale facade. The default remains an evaluated snapshot. Both original
dependency specifiers and the implementation body are copied without edits.

## Graph and deployment

The only public route under test is the package's ESM export `zod/v3`, which
selects `v3/index.js`. The fixture declares the following complete static
closure. Paths in the table are relative to `v3/`; targets are listed in
source order, with repeated edges preserved.

| Module                                    | Static targets                                                                                                                |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `index.js`                                | `external.js` (namespace import), `external.js` (star re-export)                                                              |
| `external.js`                             | `errors.js`, `helpers/parseUtil.js`, `helpers/typeAliases.js`, `helpers/util.js`, `types.js`, `ZodError.js` (star re-exports) |
| `errors.js`                               | `locales/en.js`                                                                                                               |
| `helpers/parseUtil.js`                    | `errors.js`, `locales/en.js`                                                                                                  |
| `types.js`                                | `ZodError.js`, `errors.js`, `helpers/errorUtil.js`, `helpers/parseUtil.js`, `helpers/util.js`                                 |
| `ZodError.js`                             | `helpers/util.js`                                                                                                             |
| `locales/en.js`                           | `ZodError.js` (`ZodIssueCode`), `helpers/util.js` (`util`, `ZodParsedType`)                                                   |
| `helpers/{errorUtil,typeAliases,util}.js` | None                                                                                                                          |

The reference tree replaces the locale's two outgoing edges with one to
`locales/en-error-map.mjs`, which has the locale's old edges. The original
and reference graphs are synchronous and acyclic. Node uses ordinary ESM
resolution in isolated local trees. Each tree has the same upstream root and
`v3/` package metadata and the same [application consumer](zod_v3_static_graph/consumer.mjs)
outside the package. It imports only `zod/v3`. Zod's shipped schema, error
configuration, and parse modules exercise the error map through their original
imports. The test covers that finite route and its observed behavior under Node;
it makes no claim about other package exports, CommonJS, browsers, different
resolution or evaluation environments, or equivalence for all inputs.

## Observations

On [Node 22.22.2](https://app.buildbuddy.io/invocation/e051f352-c297-4e16-bc9f-7fff092f80b2),
the original and independently assembled reference tree match on all 110 public
namespace keys (including the namespace symbol), descriptor flags and member
types, and on selected alias identities. The consumer observes the same
missing-field message (`Required`), wrong-type, string-size and enum messages,
error-map override and restore, and valid parse result.

- **Wrong initialization order:** Place the same default snapshot before the
  `const errorMap` initializer in the moved module and forward that default
  through the facade. The same static graph reaches a read of `errorMap` before
  initialization, so the application import raises `ReferenceError`.
- **Wrong dependency cell:** Import the provider's existing `util` export under
  the local name `ZodParsedType`. The normalized graph and public namespace
  shape are unchanged. Missing input now produces
  `Expected string, received undefined`, while a number supplied for the same
  string field still produces `Expected string, received number`.

The ordinary CLI receives an explicit manifest naming the public entry
`v3/index.js` and all ten original modules. Both a fresh publication request
and `--check-output` against the reference tree exit 1 with
`reason_code: no_constructed_rule` and
`reason: no constructor supports multi-chunk deployments`. The guard runs
before analyzing the placement. The fresh output path stays absent and the
reference tree stays untouched. There is no CLI-emitted artifact or certified
readback; the runtime observations come from Node reopening the fixture's
reference files on disk.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:zod_v3_static_graph_test --test_output=all
```

## What stops after dispatch

The [linked diagnostic run](https://app.buildbuddy.io/invocation/94c3e09e-ec8a-4ff6-b257-8a7a32cdf00a)
reuses the fixture's original and reference files and its authored manifest and
placement. It feeds their annotated ESTree to the existing Lean deployment
decoder and runs a separate module-by-module census with the empty profile.
The JSON test artifact, `zod-v3-linked.json`, includes source ordinals for
every unit, static request records, both schedules and the Lean results.

1. **Placement boundary:** `LinkedInput.decodeDeployment` first returns
   `v3/locales/en.js: original import or export unit 0 must stay in the entry`.
   Unit `#0` requests `../ZodError.js`; `#1` requests `../helpers/util.js`.
   Both really move in this placement. The decoder's subsequent readback
   check also expects every original provider request in the old entry. The
   reference tree still reaches both providers in the same order, but the
   existing decoder does not prove this relocation rule.
2. **Public link boundary, measured independently:** `index.js#0`
   (`import * as z`) is refused with `expected a named or default import`.
   The fixed `external.js#0` independently fails with
   `unsupported statement ExportAllDeclaration`. There are 19 original
   request records: 12 imports, seven star re-exports, and no named
   re-export-from records. The index has two separate requests for the same
   module (namespace import and star re-export). The candidate has 20 records:
   the original two locale requests move to the worker, and the facade adds
   an import from the worker. The named export lists
   `index.js#2` (`{ z }`), `errors.js#2` (`{ defaultErrorMap }`), and
   `helpers/typeAliases.js#0` (`{}`) add no edges.
3. **Execution boundary, measured independently:** The dependency-free
   `helpers/util.js` is the first module in the graph's order. Its complete
   decoded module links and its `var util` row runs in isolation; the call
   in row `#1`, `(function (util) { … })(util || (util = {}))`, stops the
   existing probe with `deferred function : unsupported global Object`.
   The body is needed eagerly to install `util.arrayToEnum`, used by
   `ZodParsedType` and `ZodIssueCode`. The existing ECMAScript census profile
   changes the first deferred reason at this same row to
   `unsupported global Number`; that comparison does not change the
   deployment contract or demonstrate execution under that profile.

The synchronous evaluation order implied by the actual requests is
`helpers/util.js → ZodError.js → locales/en.js → errors.js → helpers/parseUtil.js
→ helpers/typeAliases.js → helpers/errorUtil.js → types.js → external.js → index.js`.
The candidate inserts only `locales/en-error-map.mjs` immediately before
`locales/en.js`. The original and candidate have no cycles; the repeated
edge from `index.js` to `external.js` does not cause a second evaluation.
These are request-derived schedules. Lean did not construct either complete
linked program.

| Original file (under `v3/`) | Lowered outer units / executable units | First independent stop or deferred callable                                             |
| --------------------------- | -------------------------------------: | --------------------------------------------------------------------------------------- |
| `index.js`                  |                                  1 / 1 | Record `#0`: namespace import. The isolated `#3` default snapshot lowers.               |
| `external.js`               |                                  0 / 0 | Record `#0`: star re-export.                                                            |
| `errors.js`                 |                                  3 / 3 | None reported.                                                                          |
| `helpers/parseUtil.js`      |                                10 / 11 | `#6` (`INVALID`): `Object`; `#12` (`isAsync`) defers on `Promise`.                      |
| `helpers/typeAliases.js`    |                                  0 / 0 | Only `export {}`; isolated execution completes.                                         |
| `helpers/util.js`           |                                  6 / 6 | `#1` defers on `Object` and is called during initialization.                            |
| `helpers/errorUtil.js`      |                                  2 / 2 | Isolated execution completes.                                                           |
| `ZodError.js`               |                                  3 / 4 | `#2` (`quotelessJson`) defers on `JSON`; `#3` (the class) refuses on `Object`.          |
| `types.js`                  |                              148 / 150 | `#6` (`handleResult`) defers on `Error`; `#26` refuses on `RegExp`.                     |
| `locales/en.js`             |                                  2 / 2 | `#2` allocates the arrow with a deferred body (`Date`); `#3` lowers as a snapshot read. |

The census excludes request/metadata rows. “Lowered outer units” can include
deferred callables. It does not assert that nested bodies, imports, or
public functions are executable. Independent candidate parses likewise lower
the worker's declaration (with the same deferred body) and the facade's
snapshot. They do not check the linked cells of either complete graph.

A language-only next step is to represent and resolve the fixed barrel's
star re-exports. Keep those module requests in evaluation order and resolve
star names to the provider's existing cells, excluding `default` and
handling collisions and identical resolutions. The concrete check here is
that `zod/v3`'s `defaultErrorMap` reaches
`index.js → external.js → errors.js → locales/en.js:default`. Both
`errors.js` and `helpers/parseUtil.js` must still import that same facade
snapshot cell. In the candidate, the snapshot reads the worker's distinct
`errorMap` lexical cell, whose closure must capture the unchanged
`ZodIssueCode`, `util` and `ZodParsedType` provider cells. Also required
for the public entry: its `z` binding and its separately evaluated default
snapshot must hold the same `external.js` namespace object, with live named
exports. A fixed barrel can be addressed without assuming anything about
`Object`, `Number`, or `Date`; it will not resolve the namespace import,
prove the placement, or make the original graph executable under the
empty profile by itself.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:zod_v3_linked_diagnostic --test_output=all
```
