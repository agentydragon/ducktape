# Cyclic export-cell observations

Draft [PR #16539](https://github.com/agentydragon/ducktape/pull/16539) contains
the partial source-derived repair and the abandoned broad candidate.

## Reproduction and missing obligation

The real CLI accepts moving `callback` to its own imported logical module:

```javascript
// app.js
import { inspect } from "./observer.js";
inspect();
export var callback = () => 1;
```

```javascript
// observer.js
import { callback } from "./app.js";
export function inspect() {
  console.log(typeof callback);
}
```

Node prints `undefined` for the source and `function` for the accepted emitted
entry. The pre-fix CLI regression failed in invocation
`50346899-4cde-4718-bf11-a3ec46968763`. A `let` variant catches `ReferenceError`
in the source and prints `function` after the same invalid relocation.

`available_export_callbacks` discovers only values present at the current source
prefix. That is appropriate for callable availability, but a cyclic importer can
read an exported cell before it contains any callable. No such cell observation
is promoted at unknown execution. Existing graph handling can preserve the
required order once it receives the read: `promote_top_effects` records
`uninitialized_reads`, and `graph/build.rs::promote_at_init_calls` pairs the read
with the reverse initialization order.

## Candidate result and downstream conflict

Candidate `c0f1e2324817a6436a155f9e3a1d693a4085ce8f` promotes reads of every
cell allowed by the existing export-reentry policy before possible callback
execution. It preserves callback availability, provenance, and invocation budgets.
Exact-head invocation `f9e41538-377a-4305-a2a7-883a3ff45d92` passed all 12 requested
e2e suites, including all five publication fixtures and the `var`/`let` cases.

The same invocation failed `excalidraw:canonical_summary`: `DebundleAnalyze` and
`DebundleProjectAnalysis` succeeded, but `DebundleApply` refused the editor chunk.
One atomic group contains 868 source units across 26 destinations. Its witness
pairs `unit:19` in the residual with `unit:475` in `packages/excalidraw/appstate`:

- `unit:19 -> unit:475`: direct eager read of exported binding `Ii`.
- `unit:475 -> unit:19`: binding timeline for `Ii`, `writer_after_reader`, with
  observed writer and `opaque_callback_invocation` fallback reader.

The editor export policy is `conservative`. Loader closure is open: two missing
`loader_result_for_evaluating_module` host facts and six `unbound_require` sites.
There is also a `non_esm_chunk` static-closure blocker. The new fences therefore
cannot be dismissed as impossible under the current source/spec assumptions.
The frozen spec, host premises, budgets and gate were not changed.

Hosted CI on the same head failed too (invocation
`b1053465-c599-5773-9345-ae9d7f93599f`): 273 tests passed, five tests failed, and
three targets failed to build. Pre-commit and Nix wheel/import checks passed.

## Partial source-derived repair

The current `ForeignCallSummary` is a positive no-reentry/allocation capability,
not an incomplete may-read summary. `adapters/provider.rs::seal_callable` admits
only allocation or closure bodies and requires all captures to be source-private
`var` bindings. The observer's console call fails body admission; its imported
`callback` also fails capture admission. Widening that admission would incorrectly
certify impure code.

`ForeignCallSummary::provider_cell_reads` contains provider-local names and is
only produced after this strict sealing. Application substitution refuses to use
such a summary as an effect receipt when the set is nonempty. Similarly,
`DeferredRecordMember::captures` explicitly carries provider-local provenance;
returned records activate `ForeignDeferredRecord`, which conservatively promotes
all importer cells rather than projecting exact read identities.

The successor transports a separate `CallableSummary::may_read_importer_cells`
set. `ModuleScanVisitor` records direct top-level function-declaration import
references in the canonical shallow walk. Program-summary construction resolves
the provider's imported name back through the caller's export alias to its local
binding. Application execution promotes those SWC binding IDs before either call
transfer, under fallback provenance. Existing unknown effects, sealed summaries
and completeness stay independent. The saved callable payload carries this set.

Descendant bodies are deliberately included conservatively. A returned closure
that reads an import does not establish an observed read during its factory's
initialization. The CLI control supplies a valid `source_only` initializer
contract for that allocation and checks it is not contradicted by an observed
read. The core cyclic observer rejection has no such author contract.

This partial path leaves the broader conservative-policy gap open. For example,
the same observation can be reached through a global instead of a direct named
import:

```javascript
// app.js
import "./observer.js";
globalThis.inspect();
export var callback = () => 1;
```

```javascript
// observer.js
import { callback } from "./app.js";
globalThis.inspect = () => console.log(typeof callback);
```

The named-call projection has no callable import at this callsite. Neither an
empty may-read set nor current callback availability proves that future export
cells are unobservable. Transitive calls, re-export chains, namespace calls and
non-declaration callable bindings are also outside this partial projection.
Close those lanes with behavioral CLI refusals and unchanged frozen canonical
acceptance; do not certify arbitrary opaque execution from this bounded repair.
