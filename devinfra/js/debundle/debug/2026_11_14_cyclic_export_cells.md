# Cyclic export-cell observations

Merged [PR #16539](https://github.com/agentydragon/ducktape/pull/16539) contains
the partial source-derived repair. The broader candidate described below was
abandoned because it rejected the frozen Excalidraw spec.

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
`uninitialized_reads`, and `graph/build/at_init.rs::promote_at_init_calls` pairs the read
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

## Partial source-derived repair

The current `ForeignCallSummary` is a positive no-reentry/allocation capability,
not an incomplete may-read summary. `execution_index/provider.rs::seal_callable` admits
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
set. The shared value-flow collector records lexical access witnesses and
composes them across source-known calls, parameters, returns and local re-export
aliases. Its retained-descendant projection resolves source cells back to caller
exports. Application execution promotes those SWC binding IDs before either call
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
cells are unobservable. Namespace calls and unresolved callable identities are
also outside this partial projection. The shared source-access relation and its
bounds are documented in <../docs/inference.md#shared-lexical-access-summaries>.

The standalone flat spec for those two source files is:

```yaml
inputs:
  input_root: snapshot
  js_list_path: snapshot/js-files.txt
logical_modules:
  static/app:
    callbacks/value:
      members:
        - name: callback
          selector:
            binding: { name: callback }
import_order:
  static/app: [callbacks/value]
unassigned_mode:
  static/app: { kind: inline_in_entry }
materialize_logical_modules:
  prune_other_chunks: false
  target_dir: modules
write_js_tree: { out_dir: out }
```

Place the files at `snapshot/static/app.js` and
`snapshot/static/observer.js`, plus `snapshot/package.json` containing
`{"type":"module"}` and `snapshot/js-files.txt` containing the two lines
`static/app.js` and `static/observer.js`. Run the ordinary CLI with
`debundle run --spec repro.yaml` (no `--emit-on-rejection`), then compare
`node snapshot/static/app.js` and `node out/app/static/app/entry.js`.

On `devel` commit `fc6336654ba14c2e87b1c814d7d911ae3d5cb020`, the
standalone spec above was accepted by the ordinary CLI in BuildBuddy invocation
`1cb62972-88b5-4fb1-b916-1aad7aa883c1`. The equivalent RBE fixture, with
generic host and no author purity, commutation, or effect premises, was
accepted in BuildBuddy RBE invocation
`0584c531-02f4-47f3-97f4-4a7813727863`. Node printed `undefined` from the
source and `function` from the emitted entry. The ordinary-gate verdict and
Node comparison with `--emit-on-rejection` also passed in invocation
`d9881b0a-fa98-434c-96bc-64c6ab5a4320`. This is a current engine soundness
defect independent of false author premises, distinct from the fixed direct
named-import route in <../e2e/callbacks/availability/publication_order.rs>.

The contract boundary and the separate false-author-premise control are in
<../docs/design.md#soundness-boundary-and-regressions>; follow-up ownership is
<../TODO.md#cyclic-export-cell-observation-soundness>.

Close those lanes with behavioral CLI refusals and unchanged frozen canonical
acceptance; do not certify arbitrary opaque execution from this bounded repair.
