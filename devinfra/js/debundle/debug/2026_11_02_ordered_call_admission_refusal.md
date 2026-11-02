# Ordered call-admission refusal

An ordered call whose operands are already evaluated can be declined before its
effect transfer runs. The saved graph currently reports the conservative call
site, but the generic `opaque_execution` reason does not show whether admission
failed for an unknown target, multiple targets, a non-local target, a tainted
local target, or an unsupported receiver. This can send an investigation toward
the wrong part of call analysis. A completed effects-only call and an
incomplete invocation are separate cases.

The precision-loss reason now has an optional typed
`ordered_call_admission_refusal` with those bounded categories. It is attached
to the existing call-site fallback loss. Unknown-target classification,
target reachability, fallback effects, and call admission retain their existing
rules; no target identities or source expressions are persisted or replayed.

No local tests, builds, corpus jobs, profiles, or measurement runs were
performed for this diagnostic-only candidate.
