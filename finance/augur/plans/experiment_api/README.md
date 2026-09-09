# Experiment programs for a proposed Augur library

Design sketches, 2026-09-08. The Python programs below describe APIs we would like to
use; `proposed_augur` names the [proposed modules](#proposed-building-blocks), not an
implementation. These are code for review, not runnable
experiments, and produce no results in this change. Paths to historical records,
model artifacts, tax configurations, and private inputs are supplied by the caller.

Read the experiments before deciding which abstractions to implement:

| Program                                           | Question                                                                                 | Distinct demand on the library                                           |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| [Market-model comparison](model_comparison.md)    | Which models predict better, and do their differences change the policy we would select? | Rolling held-out forecasts; cross-model policy evaluation                |
| [Trinity](trinity.md)                             | How do withdrawal rate and allocation affect historical success?                         | Overlapping historical windows, explicit annual conventions              |
| [Guyton–Klinger](guyton_klinger.md)               | How do adaptive rules change sustainable withdrawals and purchasing power?               | Path-dependent spending and portfolio management; conditional statistics |
| [Vanguard-style dynamic spending](vanguard.md)    | How does bounded spending flexibility change outcomes across allocations and horizons?   | Our own forecast paths; an executable spending function                  |
| [Allocation glide paths](glide_paths.md)          | Does changing allocation over time improve outcomes under different return assumptions?  | An executable allocation function; shortfall severity, not just failure  |
| [Forecast-driven spending](forecast_feedback.md)  | What if the spending policy periodically reassesses the plan?                            | Conditional inner forecasts, explicit policy beliefs, no future leakage  |
| [Spending and allocation](spending_allocation.md) | Which combinations of lifestyle flexibility and investments produce acceptable outcomes? | Actual lots, taxes, reinvestment, transitions, model sensitivity         |
| [Housing and investments](housing.md)             | How does buying and financing a home change the distribution?                            | Several agents, contracts, asset acquisition and disposition             |

The model-comparison row is its own experiment family, not an accessory to spending
flexibility. These are capability examples, not a commitment to match every paper's numbers.
Trinity is a useful historical reproduction target; the other studies motivate
experiments whose substitutions and assumptions are visible. Proprietary forecast
replication is not a goal. Every row is a distributional experiment with a parameter
sweep. Financial-mechanics acceptance tests support these experiments; they are
not additional user experiments.

[Exogenous models](exogenous.md) shows loading, fitting, and sampling in the
experiment shell. [Further studies](studies.md) explains the broader selection and
which additional capabilities those studies would exercise.

## Proposed building blocks

The `.pyi` files below are interface sketches, not a runnable package. Each has a
short responsibility docstring. Signatures make the connections concrete without
specifying every product, tax rule or implementation detail. The module boundaries
are a proposal to discuss, not a commitment to Python or a matching set of Rust crates.

| Module                                        | Responsibility                                                             |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| [money](proposed_augur/money.pyi)             | Currency amounts, real budgets and purchasing-power bases.                 |
| [instruments](proposed_augur/instruments.pyi) | Product identities/terms shared by markets, books and policies.            |
| [accounting](proposed_augur/accounting.pyi)   | Actors, books, lots and balanced financial events.                         |
| [taxes](proposed_augur/taxes.pyi)             | Statutory consequences in accumulated filing-unit context.                 |
| [contracts](proposed_augur/contracts.pyi)     | Existing obligations, including mortgages and leases.                      |
| [data](proposed_augur/data.pyi)               | Author-selected datasets, vintages, loading and alignment.                 |
| [markets](proposed_augur/markets.pyi)         | Fit/condition/sample models; bind outputs to products and observables.     |
| [state](proposed_augur/state.pyi)             | Assemble financial situations; preserve complete continuation checkpoints. |
| [policies](proposed_augur/policies.pyi)       | Executable rules and reusable policy factories; propose, do not settle.    |
| [simulation](proposed_augur/simulation.pyi)   | Advance timelines and settle decisions using shared financial mechanics.   |
| [results](proposed_augur/results.pyi)         | Traces, experiment-owned outcome reductions and forecast scoring.          |

These are composable pieces, not eleven mandatory steps. Model-fit comparisons
use data, markets and scoring without simulating an investor. Trinity adds a
synthetic opening book, policies and execution with explicit no-tax rules. Personal
planning supplies actual lots and tax state; housing also adds contracts and
counterparties. Experiment shells own their input loading, parameter sweeps,
model/policy selection and presentation; there is no new experiment-framework object.

## How to read the code

The proposed vocabulary is deliberately shared across the programs. Its spelling is
negotiable; the behavior the caller asks for is the review target.
Imports name the defining module so the programs show which building blocks they
need. This is a design pass, not a typechecked API contract or financial validation.

- An **instrument object** identifies one financial product and its terms. The book,
  market binding, and trading policy reference that same object. A binding states
  the chosen price/cashflow approximation; it cannot independently change the
  product's currency, distribution character, or contractual rights.
- A **market binding** maps input observations to fitted variables and sampled
  variables to instruments and named observables. A bound model's `condition`
  produces a dated forecast; that forecast's `sample` draws paths. Historical
  `windows` enumerates paths from an explicit record. A
  supplied `Worlds` can come from an external model. No universal calibration
  interface is required of every provider.
- **Datasets** are selected and named by the experiment author, using provider
  loaders or ordinary file reads. Shared alignment joins those selected series;
  it does not select a global evidence bundle. Fit code names its variables and
  transformations, and model bindings name their financial meanings.
- A **situation** contains the opening books, agents, contracts, calendar, and tax
  state. `Situation.investor` is a convenience for a single investor. The housing
  program constructs multiple agents directly.
- A **strategy** chooses spending and trades through executable policies. Library
  factories can return common policies; the engine does not need a closed enum of
  every study's algorithms. Parameters are data, behavior is code, and path-local
  policy memory is explicit. Spending and trading can also be coordinated.
- `simulate` evaluates one situation and an actor-to-strategy mapping over a population. Python is the
  notation here, not a decision about the implementation language. Loops in the
  experiment shell enumerate cells; the executor advances the simulated calendar.
  Each initial call starts fresh state. `resume` instead clones complete checkpoints
  and preserves unchanged policy memory. Neither operation mutates an input situation,
  checkpoint or world reused by another cell.
- A **run** exposes requested per-path statistics and a reproduction receipt.
  `run.trace(path_id)` executes the identified path with detailed capture. This
  does not depend on a web-app rollout cache.

`run.paths` is a Polars frame with one row per path. Each program requests its
columns through an `observers` mapping; these are reductions during execution, not a request
to retain the complete ledger of every path. `path_id`, `reached_horizon`,
`unfunded_withdrawal`, and `contract_default` accompany every row. Terminal values
are null for a stopped path. `total_spending_real` means spending actually paid
through the stopping date, not hypothetical future spending.

Real-money columns use the explicit `ReportingBasis`: currency, price-index identity,
and base date, preserved by continuations. A terminal wealth measure includes outstanding liabilities and accrued
taxes; it does not silently assume every asset was liquidated. An experiment
wanting liquidation value requests that separately. Exact accounting and rounding
belong to execution. Policy calculations may use approximate numeric arrays through
`RealBatch.values`; wrapping the result with `with_values` retains its basis and
path identities. Those arrays are not ledger money.

The programs show ordinary Polars reductions to make denominators and conditioning
visible. `pl.concat(rows)` yields a table indexed by the sweep parameters. Returned
`runs` allow examination of the distribution and any selected path, not just its
mean. A run receipt need not retain the full simulation in RAM.

## Shared requirements these programs exercise

### Composition must catch financial mismatches

Assembly checks more than whether a named price series exists. Every held or
purchasable product must have compatible valuation, cashflow, trading, and tax
support. A total-return index already reinvests distributions; it cannot acquire a
second dividend stream or enter a taxed account as a substitute for a real fund.
A distributing fund requires its payouts even if the caller forgot to request
them. An unsupported case fails before returning an apparent financial answer.

The same instrument can have different explicit model bindings in different
experiments. A historical index proxy and a tax-aware ETF are different products,
even when both represent broad equity exposure. A bond fund and an individual
bond likewise have different cashflows and trading behavior.

### Time and observations are financial inputs

`AnnualConvention` declares the within-year order for an annual-return study.
Trinity's ambiguous withdrawal timing is a required argument, not an engine default.
The personal programs use a dated monthly calendar and actual tax-year boundaries.

Policies receive observations available at their decision time, their own prior
decisions, and known future contracts. They cannot read the remaining sampled
future. A fitted-model policy may use a forecast conditioned on that information;
it cannot use the realized continuation. The observation surface can expose a
non-mutating funding/tax preview using shared financial mechanics; the policy must
not recreate the tax calculation to understand a decision's consequences.

### Executable policies, execution language undecided

The Vanguard program supplies a function `(observations, state) -> BudgetDecision`;
the glide-path program supplies `(observations) -> target_weights`. Values have a
leading path axis. Changing a function changes the study without extending a
central schema or teaching the engine a new named policy. Built-in policies use
the same interface. A coupled policy may return spending, allocation, and actions
together when independently composed decisions would be inconsistent.
`Strategy.from_review` admits an author's own state type and a `PolicyStep` carrying
a coordinated `Proposal`; the simpler budget/allocation adapters are conveniences.

Functions receive read-only observations, return proposals, and cannot directly
mutate books. Settlement validates and executes proposals with shared instrument,
contract, and tax rules. A lifestyle move may create a contract; a later policy
cannot erase its obligations. Requested changes and accepted changes are distinct
events. Policy state is isolated per path and follows path identities through
batching; no population averages may leak between independent timelines.

The array code illustrates batching, not a choice of NumPy over JAX, or a promise
that arbitrary Python is compilable. Scalar callbacks, batched Python callbacks
into a native engine, compiled array functions, and native functions remain
implementation candidates. Host calls once per batch/review differ substantially
from calls once per path/month. We should measure both, with realistic lots and
taxes, before requiring Rust extensions or discarding Rust. Compilation latency,
memory movement, traceability, and ease of authoring matter alongside throughput.
The current runtime remains unchanged by these sketches.

Pre-sampling assumes these investors do not change the external market. Other
agents can still exchange money, hold claims, and make decisions within the
simulation. Experiments requiring market impact would need a different execution
contract; none of these examples assumes it implicitly.

### Population identity and interpretation survive execution

Within a model, cells reuse the same worlds. Random streams are identified by
economic driver and path, so adding a scenario or an unused instrument cannot
change existing paths. Prefixing a long path set preserves its history. Comparing
different models is a sensitivity analysis; matching integer seeds does not by
itself make their worlds economically paired.

Every run receipt resolves each named dataset's source and snapshot/vintage, the
model artifact and fit window, instrument bindings, starting situation,
tax-law assumptions, policy parameters,
calendar, engine version, and path identities. Executable policies add a pinned
code revision and captured parameters; the receipt need not serialize arbitrary
closures. A report carries that receipt.
Historical overlapping-window fractions are not independent Monte Carlo
probabilities. Monte Carlo uncertainty should be computed from paired path
differences when comparing cells; model uncertainty is a separate axis.

### Outcomes belong to the experiment

An unfunded desired withdrawal, a missed contract payment, a lifestyle transition,
and asset exhaustion remain distinct observations. Failure does not erase an
agent's remaining house, debt, or other assets. `on_shortfall="stop"` deliberately
ends these example paths; terminal statistics identify that censoring. A later
experiment can specify a recovery process without redefining the ledger.

Published comparisons use each paper's definition of success and conditional
statistics. Personal planning reports both financial failure and the distribution
of consumption, cuts, and backstop use. It does not infer that nonzero terminal
wealth means an acceptable life, or reduce preferences to an unrequested utility
function.

## Review questions

Can an experiment author understand and change the independent variables without
reimplementing settlement or taxes? Do the examples leave important strategy
choices hidden in constructors? Which repeated assembly belongs in a reusable
financial component, and which is legitimately specific to the experiment?

Review these programs first. Comparing their requirements with today's Augur and
writing an implementation/migration plan is the next task, not part of this draft.
