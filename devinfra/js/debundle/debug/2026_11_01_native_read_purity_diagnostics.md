# Native own-data read purity diagnostics

`unknown_member` purity reasons for recognized direct native own-data reads
now carry their required slots, the slots without proof, and the matching
`debundle explain-integrity --slot` query. That command reads the saved
program-wide `native_integrity_witnesses` census and remains the source of the
blocking witness details. A direct `eval` or `with` is reported as an open
dynamic-scope census.

The diagnostic only recognizes a direct unshadowed intrinsic binding with a
syntactically static property that is present in the native own-data table.
Computed properties, shadowed intrinsic names, and unknown properties are
identified as ineligible for an exact native-slot query. Aliased or chained
receivers are not classified by this diagnostic and retain the ordinary
`unknown_member` reason. Extending those cases should use the canonical
synchronous call/value index rather than adding a second source scan.

This is report context only. It does not change the purity result, proof
acceptance, or dependency/effect ordering.
