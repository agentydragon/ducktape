# Split declarator owner coverage

## Refusal and bounded source evidence

`top_level_item_views` already lowers an ordinary comma declaration into one
owned singleton view per declarator, in source order. Before this candidate,
`OriginalInitializer::collect` refused every owned view as
`split_declaration`; it could not inspect the original parent or identify the
exact selected declarator after the cloned singleton erased that lineage.

The canonical view now retains only the original parent item and selected
declarator index. The original child and sibling count derive from that parent;
the existing execution-index enumeration supplies the source-unit ordinal.
The source collector accepts only an ordinary, non-exported variable parent,
checks that the selected original child agrees with the split view's
declaration kind/context/declare flag, simple binding identity and spans, then
applies the existing initializer syntax and runtime certificate path to that
child. Siblings retain independent source-unit effects and source order.

This does not admit exports, destructuring, side-effecting/unsupported
initializers, getter access, calls or callback execution, or reads before a
binding is initialized. The accepted source subset still needs the dedicated
`EvaluateInitializer` result, initialized-read proof, complete execution, no
unknown call, precision loss or TDZ, and the initialized binding. It does not
change getter/callback reachability, purity assumptions, global/heap facts, or
validator authority.

## Regression evidence

Before implementation, the neutral simple middle/later declarator case failed
with unknown coverage four per domain: the three comma children plus
`console.log`; the report counted three `split_declaration` refusals. Baseline
focused e2e invocation: `22c96f40-0449-482b-b1e4-581e8cc5504d`.

The focused RBE effect-TV invocation `15b08c00-c927-4a64-a207-afa20ceef32a`
passed both selected regressions. The positive case emits the three literal
declarators into separate modules, preserves their output under Node, and
leaves only the trailing `console.log` owner uncovered. Controls keep exact
unknown-owner counts: two for an effectful sibling, three for an accessor
source plus member-read initializer, two for a callback-invoking initializer,
and two for the TDZ case. The accessor and member-read owners each retain an
`unsupported_initializer` refusal; the callback call retains that refusal;
the forward read fails the existing initializer-value proof. The TDZ source
and emitted entry both throw `ReferenceError`.
