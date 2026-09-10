# Remaining input and execution cleanup

The [roadmap](roadmap.md) owns dependencies and dispatch. These are bounded
deletion criteria for gaps found in the live-reader audit, not a new framework
or a second migration plan. Remove each section with its last reader.

## Input objects

**INPUT — private lowering, typed prepared inputs.** `CompiledRun.execution_input`
is a public mutable nested wire dictionary. Common-session callers serialize it;
product projections inspect it for domain metadata, and a joint-example test
mutates it. Keep typed declarations/prepared facts authoritative; expose needed
metadata through the existing prepared-run object and keep serialization at the
native boundary. Migrate all callers, tests and file-entrypoint readers atomically.
Do not retain a public raw tree beside the typed tree, merely add casts, or add a
new executor abstraction. Consume the existing typed prepared tax records and
common outputs rather than introducing another representation.
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
This is independently landable: no MODEL/GM statistical-adoption gate and no
prerequisite edge to BIND's price/payout validation slice. Retain current factor
encoding; the deferred IDTYPES work in [the entity-ID note](typed_series_config.md)
does not gate typed observation records or require an artifact/frontend ID sweep.

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
readers. Reuse the common-session distribution, public-sale/tax and dated-bond
controls rather than restoring their configured-runner suites.
The multiple-taxpayer cases in `sim/testing/income_sources.py` need GP's scoped
multiple-actor sequencing; separate runs are not a replacement for those joint
controls. Keep the common-session product bond-value regression as actual adapter
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

The [capability backlog](future_work.md) owns the remaining questions from the old
project/simulator TODOs; the roadmap alone owns sequencing. Do not restore their
data-driven liquidity programs, second mutable TLH basis accumulator, cached
rollouts or completed implementation items.

Reconcile the remaining interface sketches and older model/actor plans with the
executable-policy/common-session contract. Review the prediction-market calibration,
interpolator and exogenous-rollout notes: separate implemented calibration contracts
(which belong in durable docs) from optional model proposals and deferred evidence
operations. Remove code-to-plan citations as those contracts graduate. These notes
must not introduce an alternative model-adoption gate beside SCORE/GM/READY.

### Older PR disposition

These are proposed dispositions, not claims that the PRs have been closed. Remove
each row when the disposition is resolved; do not preserve another history ledger.

| PR                                                          | Disposition and surviving requirement                                                                                                                                                                                                                                        |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#4624](https://github.com/agentydragon/ducktape/pull/4624) | Supersede with the current SPEC/requirements reconciliation. Preserve accountable financial histories and independent controls; drop its mandatory 100,000-path latency/memory gate.                                                                                         |
| [#5859](https://github.com/agentydragon/ducktape/pull/5859) | Keep as review-only study input, not another supported interface package. Consume its studies through STUDY/RUN/HOUSE/SCORE/ROBUST; inner-forecast continuation remains future scope. Replace sketches with runnable consumers rather than implementing every proposed stub. |

The reviewed heads retain useful questions but do not establish financial fidelity
or current interfaces. Ask the owner about unresolved intent after inspecting code
and history. Closure is a separate explicit action, not a reason to delay DOCS.

DOCS has no implementation prerequisite and does not gate other work. Each code
migration still updates its own affected README/SPEC and removes its completed
plan entries; DOCS is not permission to postpone those changes.
