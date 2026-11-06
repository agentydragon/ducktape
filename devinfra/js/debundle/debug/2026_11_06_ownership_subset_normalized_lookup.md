# Ownership subset lookup after module-path normalization

A binding allowlist for a module whose authored path has mixed case could panic
before the assignment gate. For example, the authored path `Features/View`
normalizes to `features/view` in `ModulePath`, so the subset passed the known
module check. Binding validation then indexed the raw module map with
`features/view`, which was not a raw key.

The lookup now builds one normalized path-to-module index, rejects two authored
paths that normalize to the same identity, and uses the resolved module record
for binding-name validation. This is an input-validation failure boundary;
the selector solve, analysis projection, assignment and gate are unchanged.

The same authoring workflow exposed two report-reading pitfalls: allowlist
names come from authored readable member names, while `binding_names` are input
spellings; and `unit:N` IDs are local to a chunk. The CLI guide records both.
