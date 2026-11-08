# Exact initializer call effect coverage and legacy heap display

A neutral two-call wrapper fixture exposed two independent views of the same
source owner. `dependency_graph.json.gz` continued to show
`synchronous_execution.heap_frontier.own_at_init.opaque: true` and
`binding_effect_authority: ordered_root_initializer_uncovered` after an exact
`initializer_call_effect_inputs` assertion. Those fields describe the legacy
heap frontier and the separate lexical binding-effect receipt. The new author
input does not replace either one.

The normal root effect receipt is visible in
`effect_translation_validation.json` under
`coverage.covered_owner_dependencies.initializer_call_effect_inputs`. With one
exact assertion, that list contains one reference and the remaining SCC names
only the unselected call. With both assertions, the neutral fixture emits and
retained callbacks still run later against the neighboring array mutation.
The operation-domain receipt is formed through `CompletedInitialization`, after
independent initializer purity and actual ordered execution checks. It does not
claim lexical coverage, callback timing, or a nonopaque legacy heap frontier.
