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
controls rather than restoring their configured-runner suites. The household
obligation/failure, transfer and indexed-payment controls also use the common
session; do not restore their deleted legacy suite classes. Remaining sale/tax,
cash-conservation and feature-specific suites retain their actual numerical
coverage until their own consumers migrate.
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

## Older PR disposition

These are proposed dispositions, not claims that the PRs have been closed. Remove
each row when the disposition is resolved; do not preserve another history ledger.

| PR                                                          | Disposition and surviving requirement                                                                                                                                                                                                                                        |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#4624](https://github.com/agentydragon/ducktape/pull/4624) | The current SPEC/requirements reconciliation supersedes this proposal; closure remains an owner action. Preserve accountable financial histories and independent controls; drop its mandatory 100,000-path latency/memory gate.                                              |
| [#5859](https://github.com/agentydragon/ducktape/pull/5859) | Keep as review-only study input, not another supported interface package. Consume its studies through STUDY/RUN/HOUSE/SCORE/ROBUST; inner-forecast continuation remains future scope. Replace sketches with runnable consumers rather than implementing every proposed stub. |

The remaining PR dispositions do not gate implementation or create another DOCS
project. Each migration updates its own affected README/SPEC and removes its
completed plan entries. Optional model research lives in
[the research note](market_model_research.md); implemented behavior lives in
[calibration](../docs/calibration.md) and [PE model](../docs/private_equity_model.md)
documentation.
