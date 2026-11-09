# Kernel design: what remains

Wave 2 of <convergence_landing.md> landed as L2a–L2d. The contracts that
landed (the five context traits, observations returned rather than recorded,
one `World` for both adapters, the `facts/sync_effects/` layout) are in
<../docs/design.md> § The synchronous execution kernel. This file holds the
kernel work that has no caller or no construct on devel yet. Each entry
leaves when its slice lands. `Outcome.complete` stays until the authority
flip: see <convergence_landing.md> § Wave 4, L4d.

Evidence references (`path:line`) point into `facts/sync_effects/` at the
#14552 head `839549c0f4` (tag `debundle-reference-14552`), the reference
implementation being ported.

## Forks and joins: what is left

Conditional and logical operators fork the body and `alternatives/` joins
the family when every leaf has completed; the contract is in
<../docs/design.md> § The synchronous execution kernel. Left:

- **Path-relative call receipts.** `EffectCoverageFragment::join_alternatives`
  keeps the lexical receipt only when every leaf selected the same call
  edges, so a body with a call on one arm only has no receipt. The reference
  head carries branch-only calls in a may-obligation ledger
  (`effect_coverage.rs:1913`, `call_observations`); the root seal has to
  accept a may-edge before that can land.
- **Root admission.** A root initializer whose only reason for the ordered
  lane is a conditional or logical operator still takes the flat lane
  (`roots/operands.rs`, the `ordered_initializer` families). Admitting it
  needs a typed loss for an unproved member read at the root instead of the
  remainder summary, or legacy-accepted roots regress.
- **Other fork producers**: optional calls and member access, `if` regions.
  They reuse `LocalSuspension::Fork` and the same join.
- **The receiver-flow join** in `execution_index.rs` stays a separate
  lattice.

`World` owners that have no devel reader yet land with their first reader:

- `fallback`;
- `native.prefix_native_retained_*`;
- `classes.active_construction`;
- `descriptors.{copy_results,private}`.

## Kernel rules waiting for their construct

- **Regions**: the kernel allocates and restores cells. The only refusal is
  `has_unscoped_lexical_bindings`. This waits for `if`/`try` region lowering.
- **Catch bindings**: `instantiate_catch_binding` and `leave_catch_binding` are
  kernel rules. This waits for `catch` lowering.

## Provider execution

The provider adapter (`ProviderContext`) is in `adapters/provider.rs`; its
only driver is the allocation-only query in `strict_provider.rs`. Provider
execution becomes that one adapter over the kernel, replacing the query's
private executor and the reference head's `provider_activation` lane.

- Its `World` is the caller's fork when the provider body runs inside a
  caller transfer, or a fresh `World { scope: provider }` when sealing a
  summary.
- `scope` alone decides address admission, effect-sink isolation and the
  pooled-region refusal in `operation_support_receipt`.
- Summary substitution (`interpreter/provider_graph.rs:35` at the reference
  head) stays as the fast path tried before the adapter runs.
- The flat lane `invoke_with_cells` is deleted for bodies with an ordered
  plan once its four gates are covered (its `CLEANUP` in `interpreter.rs`).

This lands with Wave 3 slice 3 (provider activation).

## Root phases over `World` owners

`roots/{operands,body,assignment}.rs` take `&mut Executor` because the
evaluator methods they call own the whole executor. Each phase names the
`World` owners it mutates (`&mut world.publication, &world.module, ...`, as
split borrows) once those methods take split borrows themselves.
`roots/finalize.rs` already does this.
