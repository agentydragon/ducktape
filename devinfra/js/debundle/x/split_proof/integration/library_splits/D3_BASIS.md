# The whole D3 Shape basis curve module

The root npm lock pins `d3-shape@1.3.7`. This test passes the complete shipped
`src/curve/basis.js` (1,436 bytes) directly to ordinary `lowersplit`.
It runs the unchanged shipped `src/curve/bundle.js` (1,084 bytes) and checks
both SHA-256 digests. The target has no imports. Its only free name is
`NaN`, the immutable Number constant already represented by CoreJS. The
caller needs no default drawing context: the test supplies one and takes
`bundle.beta(1)`'s shipped `new Basis(context)` path.

| Original ordinal | Statement                              | Ordered helper request | Prototype request |
| ---------------- | -------------------------------------- | ---------------------- | ----------------- |
| 0                | `export function point`                | `spline.mjs`           | `basis.js`        |
| 1                | `export function Basis`                | `basis.js`             | `basis.js`        |
| 2                | `Basis.prototype = { … }`              | `basis.js`             | `spline.mjs`      |
| 3                | Anonymous default function declaration | `basis.js`             | `basis.js`        |

The shipped caller returns a `Basis` whose context is the original
object. Drawing three points produces a move, line, two Bézier calls,
and final line, all on that same receiver. When the supplied Bézier
method throws a sentinel on the third point, the same sentinel escapes.
The point counter has already reached 3, but the previous coordinates
are still 0 and 6. These observations invoke the original helper
without requiring a browser, native array method, or host call.

The authenticated decoder recognizes all four
rows and all three public bindings: `point`, `Basis`, and `default`.
The ordinary CLI refuses both specs at `all-outcomes`, creates no output,
and reports the narrower ordered-helper admission condition. The indexed
inline probe instead reports `expected simple top-level declaration rows`.

With the linked export table fix in [#20666](https://github.com/agentydragon/ducktape/pull/20666),
the ordered plan keeps `[0,1,2,3]` and retains `Basis` and the hoisted
`default` cell at the entry. Moving the inline export leaves `point` in the
helper, so the entry interface does not match. The prototype plan orders
`[2,0,1,3]` and resolves all three entry exports to the original live
cells. These checks are on the plan model; the CLI does not publish files.

The shared evaluator reaches the actual `Basis.prototype.point` method
at fuel 256 after initialization. On the original source it reports
`deferred function point: unsupported switch fallthrough`. The method's
third-point case calls `lineTo` and continues into the default case,
which calls the spline helper. Equal deferred outcomes do not establish
that the second call happens or that a throw prevents the later coordinate
updates. The prototype-only layout is the smaller next proof target:
cover the original fallthrough and justify running the prototype write
before two already-instantiated function declarations. The workload needs
no input-import or deployment extension.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/library_splits:d3_basis_test --test_output=all
```
