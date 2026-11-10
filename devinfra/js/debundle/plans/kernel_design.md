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
  remainder summary, or legacy-accepted roots regress. New admissions use the
  attempted lane (<convergence_landing.md> § Root admission).
- **Field reads under a fork.** An own-data read leaves the initialization
  domains unknown (`EffectCoverageFragment::lexical_operation`), so a test on
  a record field forks but its owner is not initialization-covered.
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

Landed with Wave 3 slice 3 PR 1: the provider adapter is the one place a
provider body runs (`ProviderContext::seal_{callable,class}`), and summary
substitution is the fast path with an effect receipt
(<../docs/design.md> § The synchronous execution kernel).

What remains is running a provider body inside a caller transfer, for bodies
without a summary (a body that calls another export, writes a provider cell or
invokes an argument):

- An importer holds summaries only. The adapter needs the provider's
  `ExecutionIndex` in the importer, which is the reference head's provider
  program registry (`execution_index/provider_program.rs`) and its saved
  identities (<convergence_landing.md> Wave 3 slice 6).
- With it, the adapter gains a second constructor over the caller's fork.
  `World.scope` already decides closure addresses and literal writability; the
  heap-cell sink and the settled/writable answers in `OwnDataContext` are the
  adapter's hardcoded isolation today and become decisions on `scope`. The
  pooled-region refusal in `operation_support_receipt` lands with regions.
- Summary substitution stays the fast path tried before the adapter runs.

The flat lane `invoke_with_cells` still takes bodies with an ordered plan
behind four gates; its `CLEANUP` in `interpreter.rs` names the kernel rule each
one waits for.

## Root phases over `World` owners

`roots/{operands,body,assignment}.rs` take `&mut Executor` because the
evaluator methods they call own the whole executor. Each phase names the
`World` owners it mutates (`&mut world.publication, &world.module, ...`, as
split borrows) once those methods take split borrows themselves.
`roots/finalize.rs` already does this.
