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

## Forks and the driver

```rust
pub enum ForkPolicy { Refuse, Explore }
// ControlContext::fork_policy(&self) -> ForkPolicy
```

`kernel::driver::drive` gains `LocalSuspension::Fork(children)`. Under
`Refuse` the continuation refuses branching. Under `Explore` the driver
returns `Family::fork(children).bind(|child| drive(ctx, body, child))`. A
`Family` carries one `World` snapshot per leaf and is joined by
`alternatives/` at the resume point.

- Scalar consumers refuse forks: the retained-closure proof query and
  allocation-only expression evaluation.
- The application and provider adapters explore them.
- "Any refused leaf refuses the family" is the caller's
  `Family::all_completed()` check, not a driver variant.

The first fork producer is the conditional/logical IR slice
(<convergence_landing.md> § IR growth).

## Join: `alternatives/`

`World::join` (`world/join.rs`) has landed with its strict fast path, its
per-owner `Join` and its `Joined`/`JoinRefusal`, but nothing calls it yet. Its
first caller is `alternatives/`, which comes with the first fork producer.

- The caller maps `refused` to the remainder fallback
  (`RequiresRemainderFallback`), never to an up-front fork refusal. Branches
  run so that their divergence is known.
- Of the reference head's joins, `transfer_alternatives::join_execution_states`
  (`:1168`) folds into one `ExecutionState::join` in `alternatives/`, together
  with its `join_native_retained_reference_maps` and `join_callback_fallback`.
- Three joins stay because they join other lattices:
  - `EffectCoverageFragment::join_alternatives` (`effect_coverage.rs:1913`;
    covered iff every path covers);
  - `transfer_alternatives::join_outcomes` (`:807`, the `Outcome` join with
    `join_own_property_store_receipts`);
  - the receiver-flow join in `execution_index.rs:4765`.

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
- **Forks**: a `Step` fork is refused by scalar consumers and explored by the
  two adapters, per `fork_policy` (§ Forks and the driver).

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
