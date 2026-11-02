# Commutation proposal reports omitted residual component identity

When `spec propose-commutations` reported `pairs_cannot_break`, JSON and text
showed the aggregate residual module count and blocker categories but not which
cyclic components remained. The reports now list the canonical member paths
for each remaining component in deterministic order. `residual_modules` stays
the sum of those component sizes; `pairs_break` has no residual-components
field. This changes report visibility only. The synthetic proposer CLI suite
checks both output formats; no canonical graph was rerun.
