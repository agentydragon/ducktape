# Domain-first Python convergence

The priority is the domain/object model and usable experiment APIs, not large-N
performance. Prefer Python where it makes financial entities, steps and policies
clearer to compose and inspect. This is not a rewrite benchmark contest. The
[roadmap](roadmap.md) owns dispatch and dependencies; remove completed work here.

## Fix the public result boundary first

**IDENT** makes `rollout_id` stable identity throughout execution results.
Selection `[42, 7]` means original paths 42 and 7 occupy local columns 0 and 1;
positions are internal, not a second caller-supplied identity. Metrics own their
ordered ID axis, and a projection selects by ID without independently supplied
column/ID arguments. Event frames currently call the original ID `rollout_index`;
rename that field and every caller atomically. Retain source identity even for a
trace whose event frames are empty. Compiler/market-array positions remain local
indices, not a reason to rename unrelated sampling algorithms.

**RESULT** replaces public `Finished.rollouts_json` with typed rollouts, compact
summaries, books, receipts and explicit stop variants. Decode once at the boundary
or expose typed native results; do not retain a raw dictionary tree beside the
typed tree. Reuse existing columnar event frames for traces instead of inflating
all event rows into a second record hierarchy. Preserve dense/forensic books and
journals where real consumers need them. Ordinary mappings remain mappings;
record/variant structure does not remain `dict[str, Any]`.

All existing result consumers, including actual CLI paths, migrate in these PRs.
No alias shim, raw-dictionary alternative, cast-only typing or new evaluator.
Reordered/subselected IDs, eventless paths, early stops, exact money, optional
capture and selected replay are acceptance cases. Private transport serialization
is allowed; it must not leak into domain code or be mistaken for financial logic.

## NOMCOUPON: bounded contractual-term cleanup already dispatched

Nominal coupons on the currently supported fixed-rate, fixed-principal contracts
are invariant across paths and coupon dates. Reuse the existing Python
`sim/bonds.py::coupon_amount_quanta` calculation during compilation, make its
rounding agree with the canonical PPB/face-quantum terms, and lower one
authoritative fixed coupon payment term. Both configured execution and the common
action session consume it. Delete the corresponding native nominal calculation
in `engine/securities.rs::bond_coupon`, updating all builders, bindings and
observations atomically. Do not keep two independently editable amounts/rates
that can disagree about the payable amount.

Acceptance includes independent exact rounding/nondivisible-period cases,
zero-coupon contracts, actual coupon cash/tax outcomes and maturity timing in
both consumers. Preserve observable contract terms intentionally. TIPS remains
separate: its changing principal and period-rate rounding are not the same
calculation. Do not fold an unverified TIPS correction into this migration.

This slice is justified by one clear contractual payment term and removal of a
duplicate calculation, not a performance target. It needs no benchmark gate,
new callback or deferred housing/PE migration. It must not set the agenda for
subsequent work merely because other small numerical functions are easy to port.

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

RESULT does not finish typing the world: `CompiledRun.execution_input` still
exposes the lowered transport dictionary. Subsequent domain work should keep
typed declarations/financial state authoritative and move serialization to its
boundary, not make every consumer inspect nested wire keys or mirror every native
type just to preserve the current encoding.

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
