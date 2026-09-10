# Remaining input and execution cleanup

The [roadmap](roadmap.md) owns dependencies and dispatch. These are bounded
deletion criteria for gaps found in the live-reader audit, not a new framework
or a second migration plan. Remove each section with its last reader.

## Input objects

**TAXINPUT — prepared tax records.** `sim/compiler/tax.py::TaxCompileOutput`
builds padded profile/jurisdiction/bracket arrays and counts; its production
consumer, `sim/compiler/execution.py::_tax_profiles`, reconstructs records.
Produce typed, variable-length prepared tax records directly and consume those
same records at lowering. Delete the padded intermediate arrays/counts and the
reconstruction. Preserve exact thresholds, ordering, exemptions and existing
tax controls. This is preparation cleanup, not new tax law or a port of annual
assessment; neither GT nor a performance comparison gates it.

**INPUT — private lowering, typed prepared inputs.** `CompiledRun.execution_input`
is a public mutable nested wire dictionary. Common-session callers serialize it;
product projections inspect it for domain metadata, and a joint-example test
mutates it. Keep typed declarations/prepared facts authoritative; expose needed
metadata through the existing prepared-run object and keep serialization at the
native boundary. Migrate all callers, tests and file-entrypoint readers atomically.
Do not retain a public raw tree beside the typed tree, merely add casts, or add a
new executor abstraction. Consume TAXINPUT's typed records; that contract can be
stacked on before merge. Typed common outputs already exist.
INPUT need not wait for P12: still-live configured strategies can lower privately
until their last consumers move, but must not dictate the public domain objects.

**BASIS — exact opening lot basis.** `api/portfolio.py::to_initial_lots` divides
total basis into per-unit basis; `sim/compiler/execution.py::_initial_lots` demands
a representable per-unit amount, and `product/portfolio.py::_holding_position`
repeats the conversion for display. Make exact remaining total basis authoritative
through generic imports, scenario authoring, compilation and display. Preserve
quantity precision without requiring total basis to divide into a money quantum:
include a total basis of $1 over three units, partial sales and full liquidation.
Migrate all readers together, without retaining two mutable basis authorities.
This is a correction/cleanup of existing input and display, not a product feature.
It is independent of INPUT and GT. MA1 consumes this generic opening-basis
contract instead of adding a managed-account-only workaround.

**OBSINPUT — typed market observations.** Replace `VecmProviderConfig.latest_observations`
and source-specific fallback extraction in `model/vecm.py` with typed factor
observations carrying the value and required provenance. Update fit/artifact/config
producers, runtime conditioning and `fit/state_space.py` readers together; reject
incompatible/missing observations at the boundary. Retain actual observable units
and conditioning values; no alternate dict format or duplicate extractor remains.
This is the remaining artifact/observation slice of `typed_series_config.md`,
linked from BIND but independently landable: no MODEL/GM statistical-adoption gate
and no prerequisite edge to BIND's price/payout validation slice.

## P12 reader retirement

Remaining TLH/feature-rich/native-test consumers still use scheduled public sales.
Move those decisions to explicit actions as each consumer migrates; P12 deletes
the scheduled-sale schema and executor branch with the last one. Reuse the
existing public-sale/tax controls as independent financial coverage.

**ACCEPT — remaining legacy acceptance readers.** `rust/result.py::RustResult`
decodes configured forensic output into the separate
`sim/testing/simulation_result.py::SimulationResult` contract;
`rust/backend_test.py` instantiates the suites against `run_rust`. Move bounded
supported-domain suites onto the common Python action session and the existing typed
books/traces/receipts. Keep independent numerical assertions, not only
old/new equivalence; delete superseded test contracts/adapters with their last
readers. `sim/testing/security_distributions.py` is a supported single-actor
next slice: retain independent payout, issuer-exemption and sub-quantum controls.
The multiple-taxpayer cases in `sim/testing/income_sources.py` need GP's scoped
multiple-actor sequencing; separate runs are not a replacement for those joint
controls. The existing product bond-value regression remains until BONDREPORT replaces that actual adapter
coverage, rather than a weaker principal-only assertion. Native step tests remain
useful, but no test should retain an obsolete
full-run entrypoint solely to preserve its test harness. Housing/PE/harvest suites wait only for
their affected capabilities, not the whole acceptance migration. Do not discard
regressions or wrap the old runner behind the new result type.

**BENCH — feature-rich benchmark consumer.** The remaining standalone native
benchmark runs `feature_rich_case`, which includes housing, PE, harvesting and
multiple obligated actors. It is not a ready public-only migration. Preserve that
financial workload while moving its outer loop to Python after the affected
MA2/HOUSING/PE capabilities and GP's multiple-actor timing contract exist. Do not
strip holdings/claims or substitute a smaller workload to declare retirement.
Resolve only the actor sequencing this workload needs, not a universal scheduler.
Delete the native full-run benchmark driver with the migrated caller; there is
no throughput or RUNTIME gate. Other Python experiments already exercise the
public session and need not wait for this benchmark.

P12 also deletes `compiler/execution.py::_holding_pools`' strategy-derived
declarations/first-source-account choice and
`product/scenarios.py::_target_allocation_policies_from_funding_policy` with their
last configured allocator consumers. Declarations, not a strategy configuration,
must determine available accounts/instruments. APP owns the existing app's final
cutover; do not add new endpoints or financial features there.

## DOCS — reconcile the remaining trackers

Reconcile `sim/TODO.md`, the parent TODO, remaining interface sketches and linked
subplans with the settled executable-policy/common-session model. In particular,
remove advice to add data-driven liquidity programs, a second mutable cumulative
TLH basis accumulator, or policy-emitted payments as if they were not implemented.
Map genuinely unsupported capabilities to the roadmap and keep local numerical
checks under their owning tax/product task. Remove completed entries rather than
copying them into a new backlog. Audit stale planning PRs for overlap; propose
closure/supersession rather than reviving their old implementations automatically.

DOCS has no implementation prerequisite and does not gate other work. Each code
migration still updates its own affected README/SPEC and removes its completed
plan entries; DOCS is not permission to postpone those changes.
