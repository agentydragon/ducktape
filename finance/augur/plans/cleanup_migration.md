# Remaining input and execution cleanup

The [roadmap](roadmap.md) owns dependencies and dispatch. These are bounded
deletion criteria for gaps found in the live-reader audit, not a new framework
or a second migration plan. Remove each section with its last reader.

## Scope and expansion freeze

The [roadmap's library cleanup slices](roadmap.md#committed-library-cleanups-and-open-designs)
are committed directions; the composition/lifecycle mechanism is decided in the
gate note and GMETRICS still chooses recording. Do not turn these deletion notes
into an implicit decision to standardize a particular collector.

- **SCHEMA:** after the applicable COMPOSE constructors exist, migrate a real
  caller off mandatory Scenario/compiler authoring and make import construct those
  same objects. Delete vacated schemas/lowering with their readers. Keep useful
  input validation, exact quantization and reproducible artifacts; do not replace
  the giant input schema with another equivalent bag.
- **P12:** no new configured implicit strategies or experiment consumers. Existing
  supported-interface migrations can proceed without waiting for the full World
  design; genuinely new public lifecycle/composition APIs belong to COMPOSE.
- **RECORD:** after GMETRICS, remove the affected product-specific capture from
  `World.snapshot`/`World.finish` and replaced result shapes. Preserve required
  accounting facts and existing app outputs. A new global metric tuple, mandatory
  event bus, or an assertion that World must disappear is not the replacement.
- **ACCEPT:** move the current `export_results` dictionary → `decode_result` →
  `SimulationResult` frame reconstruction to direct canonical facts, where existing
  views suffice. Do not add production users or more fields to that test adapter.
  Retire it only with its actual last reader and retain independent expectations.

Supported ACCEPT slices and unrelated no-reader deletions can land while the two
design comparisons run. New output contracts wait only for their own scoped
GMETRICS decision; new domain capabilities wait only for their relevant existing
financial/timing gate. The roadmap contains the sole DAG.

## P12 reader retirement

Configured TLH, scenario controls and retained acceptance consumers still use scheduled
asset sales.
Move those decisions to explicit actions as each consumer migrates; P12 deletes
the scheduled-sale schema and executor branch with the last one. Reuse the
existing explicit asset-sale and public-sale/tax controls as independent financial coverage.

P12 also deletes `compiler/execution.py::_holding_pools`' strategy-derived
declarations/first-source-account choice and
`product/scenarios.py::_target_allocation_policies_from_funding_policy` with their
last configured allocator consumers, along with `policy/configured_allocation.py`'s
configured-policy reader. The proposer is already Python-owned and calls shared
sleeve helpers; its language port is not remaining work. Declarations, not a strategy configuration,
must determine available accounts/instruments. APP owns the existing app's final
cutover; do not add new endpoints or financial features there.

## Older PR disposition

These are proposed dispositions, not claims that the PRs have been closed. Remove
each row when the disposition is resolved; do not preserve another history ledger.

| PR                                                          | Disposition and surviving requirement                                                                                                                                                                                                                                        |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#5859](https://github.com/agentydragon/ducktape/pull/5859) | Keep as review-only study input, not another supported interface package. Consume its studies through STUDY/RUN/HOUSE/SCORE/ROBUST; inner-forecast continuation remains future scope. Replace sketches with runnable consumers rather than implementing every proposed stub. |

The remaining PR dispositions do not gate implementation or create another DOCS
project. Each migration updates its own affected README/SPEC and removes its
completed plan entries. Optional model research lives in
[the research note](market_model_research.md); implemented behavior lives in
[calibration](../docs/calibration.md) and [PE model](../docs/private_equity_model.md)
documentation.
