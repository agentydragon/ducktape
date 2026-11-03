# Commutation predictions must preserve third-statement order

## Reproduction

The proposer reported `single_pair_cut: true` and `pairs_break` for a true
no-op/read commutation. Applying its emitted entry through the ordinary CLI
left the same two-module cycle: the gate preserved an array writer's order
against that read, and an eager use closed the reverse path.

The neutral fixture allocates a private array and defines `read`, `write`
and empty `noop` functions in a provider. An observer owns
`gamma = read()` and `delta = alpha`; a mutator owns `beta = noop?.()` and
`alpha = write()`. Source order is `gamma`, `beta`, `alpha`, `delta`.
Only `beta` and `gamma` commute. Dropping their frontier edge predicts a cut,
but `alpha -> gamma` must remain as preserved order.

The confirmation run checked the proposal and actual rejected gate in
[2543293d](https://app.buildbuddy.io/invocation/2543293d-597a-47dc-af34-3d698b47397f).
The regression expecting the corrected prediction failed at the intended
`single_pair_cut` assertion in
[34c69d1b](https://app.buildbuddy.io/invocation/34c69d1b-4274-4a5e-ab39-7c26ea408736).
These are executed CLI results, not a source-only concern.

## Shared preservation and bounded failure evidence

Graph construction and the proposer use one temporal-relation closure.
Fixed constraints win over parallel inertable reasons. The immutable base
index is built once; each hypothetical premise adds only its new relations.
It does not copy full graph metadata or scan every edge for every candidate.
Co-location, rebind and local-effect edges remain outside temporal closure.

Single-pair ranking completes preservation before reporting its cut and
remaining module-pair edge count. Combined proposals need only one surviving
cycle to prove insufficiency. Stop when that witness is found, and expose
`counts_are_lower_bounds` for the partial counts and cyclic memberships.
An acyclic prediction requires completed preservation. This avoids expanding
all transitive relations of the existing long-chain budget fixture merely to
prove that some cycle survives.

Original retained blockers keep their reasons; new constraints are reported
as `preserved_order`. The ordinary gate still validates author assertions,
selectors and the final assignment. A correct cut prediction does not prove
that the proposed assertion is true.

## Validation

The four commutation resolution, gate, proposer and budget suites passed in
[e1933792](https://app.buildbuddy.io/invocation/e1933792-3664-4504-ac4d-4c0ab44a0bed).
The new regression checks the preserved-edge count, typed blocking reason,
lower-bound marker and actual rejected gate. The existing runtime tests cover
both frontier directions and overlapping assertions in both spec orders.
The long-chain test passes its unchanged 30-second proposer budget and checks
that combined preservation stops with a surviving-cycle witness. Its overall
test time includes fixture construction; no aggregate performance or memory
savings have been measured.
