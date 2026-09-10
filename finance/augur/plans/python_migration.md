# Domain-first Python convergence

The priority is the domain/object model and usable experiment APIs, not large-N
performance. Prefer Python where it makes financial entities, steps and policies
clearer to compose and inspect. This is not a rewrite benchmark contest. The
[roadmap](roadmap.md) owns dispatch and dependencies; remove completed work here.

## Choose subsequent moves by domain value

The next priority is a coherent model that experiments can compose, not a sequence
of the easiest arithmetic kernels to port. Use managed-TLH accounts and FIRE
studies to expose needed boundaries:

- World definitions declare actors, accounts, instruments, contractual terms and
  supplied market processes. An account or product is not automatically another
  strategic agent. Names and types represent economic facts, not storage axes.
- Per-rollout books hold current financial state. Observations expose what the
  actor can know; actions express decisions the actor can make. Environment/product
  processes apply their own modeled consequences through canonical accounting.
- A managed account encapsulates an approximate investment service, including
  linked realized losses and basis. It is not an ordinary index security plus an
  unrelated policy-controlled stream of tax credits.
- Study authors load/sample paths, supply ordinary Python batch policies, advance
  the common session and inspect typed outcomes. Trinity, flexible-spending,
  allocation and managed-account examples exercise this composition; they need
  neither the app nor a universal experiment framework.

Typed common outputs do not finish typing the world. The concrete
[input/reader cleanup slices](cleanup_migration.md) cover INPUT's public
`CompiledRun.execution_input` leak and the remaining P12 readers. Reuse typed
prepared tax records, exact total opening lot basis and typed conditioning
observations. These slices need not wait for a wholesale Python executor rewrite.

The `product/` shell is not a new-feature priority. Its changes should correct
existing behavior or retire legacy interfaces; experiments remain the primary
consumers driving new domain capabilities.

Move the definitions and financial steps to Python where that makes this object
model clearer, easier to inspect and less dependent on duplicated binding/schema
representations. PYSTEP acceptance is financial correctness, coherent domain
ownership and atomic migration of all callers, followed by deletion of the native
counterpart. A speedup or large-N benchmark is not a prerequisite. Do not add
reverse callbacks or a second supported evaluator merely to move a small function.

Annual tax assessment illustrates a real content dependency, not the next
mandatory port: both native runners call `engine/taxes.rs::accrue_year_end_taxes`.
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
near-term domain/API changes, Python ports, managed portfolios or FIRE studies.
Correctness tests remain mandatory; representative large-N throughput and memory
budgets do not gate this phase. Avoid unnecessary work, but do not grow an elaborate
transport/object model to save hypothetical future allocations.

When a real experiment becomes too slow, profile that workload and optimize its
bottleneck. Compare equivalent financial work and outputs, separating language,
precomputation, batching, JSON conversion and ragged/padded recording. Native
kernels remain an option, not a reason to preserve native ownership of the domain.
The historical Rust speedup and jagged-output bottleneck have not been isolated;
that uncertainty does not block choosing a better model now.
