# Reuse local-body free binding reads

The normal invocation guard rebuilt a `BTreeSet<Id>` by walking every local
operation on each invocation, then tested each read against the body's local
bindings, current environment and module bindings. The strict provider query
walked the same operations and removed local bindings again.

The canonical collector now computes the free reads once, after validating
the local assignment targets. It uses the same expression read traversal and
removes the same hygienic parameter/local binding IDs. The sorted result is an
`Arc<[Id]>`, so cloning a local body shares it. An empty result uses no separate
allocation. Normal invocation only scans this slice; it no longer allocates a read set or traverses the expression tree
for this guard. Strict provider capture collection reuses the same IDs.

The admission predicates are equivalent: requiring every source read to be
local, in the environment or at module scope is the same as requiring every
nonlocal read to be in the environment or at module scope. Provider capture
ordering stays the original hygienic-ID order. No source syntax, operation
semantics, initialization certificate or author premise changes.

This removes repeated allocation and traversal from a source-only check. No
whole-program memory or timing savings have been measured, and no profiling,
benchmark or corpus measurement job accompanies the change.
