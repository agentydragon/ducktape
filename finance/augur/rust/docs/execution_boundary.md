# Execution input

`sim.backend.compile_run` prepares a typed `sim.prepared.CompiledRun` from a
`Scenario`, materialized paths, jurisdiction rules and locations. Sessions consume
those resolved facts directly; they never reread the authoring inputs.

`sim/compiler/execution.py` owns preparation: resolve tax rules for each filing
profile, validate sampled values, and convert money, quantities and rates into
the engine's exact integer units. It does not allocate a second world model with
cashflow slots, lot masks, or precomputed obligation tensors. Python owns session
sequencing and policy proposals, including configured allocation. Native worlds
retain books, contractual cashflow/claim processing, exact transactions, taxes,
bond calculations and remaining configured property/PE mechanics. Mortgage terms,
fixed installments, active servicing and paid-interest YTD belong to Python;
outstanding principal is read from the ledger. Native mortgage inputs are immutable
payment and tax facts, with read-only capture records rather than a loan-state mirror.
Scheduled and PE
sales still select FIFO before calling canonical lot-sale accounting.

The private Rust input type is `ExecutionInput` in `rust/execution.rs`.
JSON is currently the private transport, not another authoring API. Native
validation runs before creating worlds. `sim/actions.py` and `sim/observations.py`
own public requests and current facts; private codecs decode native observations
and financial results once. Unsupported series types are refused.

The compiler lowers explicit `Scenario.holding_pools`, initial lots and configured
allocation sleeves into one execution `holding_pools` list. The native engine reads
only that list for pool declarations; account scope and observable public prices do
not depend on a strategy executing. Explicit pools permit an all-cash actor to buy
an asset it did not initially hold.

## Experiment invocation

`rust/invocation.py::write_prepared_input` persists the typed compiled run
for reproducible experiments. The caller owns paths, parameters, batch policy,
capture choices and output analysis; the canonical session validates and executes
financial actions. The file writer does not reinterpret the document or cache
execution state. Reading the artifact returns the same typed prepared records.

Bounded spending, allocation-glide and monthly actions use the in-process
`ActionSession` with Python-owned loops and policy functions; they retain
`write_prepared_input` for reproducible artifacts.

## Precision and validation

- Authored money must be exactly representable in its currency quantum.
- Sampled prices are rounded once to currency quanta; distribution rates retain
  sub-quantum precision until multiplied by holdings.
- CPI and other index levels cross as integer parts per billion. Raw-value
  checks precede quantization where rounding could hide invalid inputs.
- Tax preparation retains filing-status support checks and rejects jurisdictions
  that disagree on the taxpayer's shared capital-loss offset cap.
- Derived amounts, such as a property's building share, use the engine's checked
  integer arithmetic and rounding; intermediate products need not be exact
  currency amounts before rounding.

Tests author ordinary scenarios and run the same preparation. Test helpers may
copy or serialize the prepared value, but do not maintain another encoder or
tax-rule interpretation. Exact-money, statute, funding, lifecycle and rollout
acceptance tests exercise this same production path.
