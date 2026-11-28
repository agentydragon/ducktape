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
