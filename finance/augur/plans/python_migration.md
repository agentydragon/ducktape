# Domain-first Python convergence

The priority is the domain/object model and usable experiment APIs, not large-N
performance. Prefer Python where it makes financial entities, steps and policies
clearer to compose and inspect. This is not a rewrite benchmark contest. The
[roadmap](roadmap.md) owns dispatch and dependencies; remove completed work here.

## Choose subsequent moves by domain value

The [current boundary](../sim/DESIGN.md) already places session sequencing,
public action/observation definitions and configured allocation proposals in
Python. PYSTEP is not a backlog to move those again. Native worlds retain financial
books, settlement, tax assessment, bond math and existing contractual processing;
choose subsequent moves for a concrete domain consumer, not language coverage.

The next priority is a coherent model that experiments can compose, not a sequence
of the easiest arithmetic kernels to port. Use the opaque TLH portfolio and FIRE
studies to expose needed boundaries:

- World definitions declare actors, accounts, instruments, contractual terms and
  supplied market processes. An account or product is not automatically another
  strategic agent. Names and types represent economic facts, not storage axes.
- Per-rollout books hold current financial state. Observations expose what the
  actor can know; actions express decisions the actor can make. Environment/product
  processes apply their own modeled consequences through canonical accounting.
- The Python TLH component owns its private exposure, adjusted basis, ages and
  rounding cash. The investor reads value/basis and chooses contributions,
  withdrawals or liquidation; no cohort mirror lives in native books. Canonical
  financial execution settles cash/tax effects and retains read-only statements.
  This is a concrete reduced-form model, not a generic managed-account container.
- Study authors load/sample paths, supply ordinary Python batch policies, advance
  the common session and inspect typed outcomes. Trinity, flexible-spending,
  and allocation examples exercise this composition. MA3 adds the paired TLH
  experiment; neither needs the app or a universal experiment framework.

Reuse the typed `CompiledRun`, prepared tax records, exact total opening lot basis
and typed conditioning observations. Private lowering is not a second authoring
format. The [remaining reader cleanup](cleanup_migration.md) does not require a
wholesale Python executor rewrite.

The `product/` shell is not a new-feature priority. Its changes should correct
existing behavior or retire legacy interfaces; experiments remain the primary
consumers driving new domain capabilities.

The [TLH integration acceptance](managed_portfolio.md) covers every driver and
native-reader retirement, not only a new example. The common and configured
Python loops advance the component before investor operations, including
scheduled redemptions, regardless of a later funding failure. Verify that
integration under the revised Python session and request types before removing
MA1/MA2; MA3 remains a future paired experiment.

Move the definitions and financial steps to Python where that makes this object
model clearer, easier to inspect and less dependent on duplicated binding/schema
representations. PYSTEP acceptance is financial correctness, coherent domain
ownership and atomic migration of all callers, followed by deletion of the native
counterpart. A speedup or large-N benchmark is not a prerequisite. Do not add
reverse callbacks or a second supported evaluator merely to move a small function.

Annual tax assessment illustrates a real content dependency, not the next
mandatory port: the common and configured Python loops share the native close
that calls `engine/taxes.rs::accrue_year_end_taxes`.
It owns netting, deductions, state assessment, SALT-dependent federal reassessment,
liability creation and reset. A pure `rust/tax.rs::assess` port alone does not
replace that orchestration. Choose a complete useful boundary when a consumer
needs it; do not revive deferred housing/PE or start a comparison project merely
to delete that function.

## Sequential time, parallel worlds

Each rollout is a stateful trajectory: today's actions affect tomorrow's books,
claims, tax state and available decisions. Independent trajectories may advance in
parallel, and the public policy accepts a batch, but that does not make the future
time axis independent or require one dense whole-horizon execution kernel.

Presampling exogenous market paths remains useful; it is separate from executing
financial decisions. Keep current state separate from output recording. Domain
objects and event/receipt lists can have naturally different sizes across paths
and months. Do not pad lots, claims, actions or events into a universal shape to
justify a vectorized executor. Numeric arrays remain useful inside market models,
calculation kernels or result reductions where their dimensions genuinely agree.

## Performance later

GL and RUNTIME/GE are parked optimization work, with no outgoing prerequisite to
near-term domain/API changes, Python ports, TLH portfolios or FIRE studies.
Correctness tests remain mandatory; representative large-N throughput and memory
budgets do not gate this phase. Avoid unnecessary work, but do not grow an elaborate
transport/object model to save hypothetical future allocations.

When a real experiment becomes too slow, profile that workload and optimize its
bottleneck. Compare equivalent financial work and outputs, separating language,
precomputation, batching, JSON conversion and ragged/padded recording. Native
kernels remain an option, not a reason to preserve native ownership of the domain.
The historical Rust speedup and jagged-output bottleneck have not been isolated;
that uncertainty does not block choosing a better model now.
