# Remaining input and execution cleanup

The [roadmap](roadmap.md) owns dependencies and dispatch. These are bounded
deletion criteria for gaps found in the live-reader audit, not a new framework
or a second migration plan. Remove each section with its last reader.

## Scope and expansion freeze

The [roadmap's library cleanup slices](roadmap.md#committed-library-cleanups)
are committed directions; composition and recording are decided in
[the gate note](library_design_gates.md).

- **SCHEMA:** the gate note's
  [remaining-work graph](library_design_gates.md#remaining-work-in-dependency-order)
  lists the prepared-scenario readers left to delete. Keep useful input validation,
  exact quantization and reproducible artifacts; do not replace the giant input
  schema with another equivalent bag.
- **P12:** no new configured implicit strategies or experiment consumers.

New domain capabilities wait only for their relevant existing financial/timing
gate. The roadmap owns dependencies.

## P12 reader retirement

The configured household (`policy/configured_household.py`) and the acceptance
suites that script sales still use scheduled asset sales.
Move those decisions to explicit actions as each consumer migrates; P12 deletes
the scheduled-sale schema and executor branch with the last one. Reuse the
existing explicit asset-sale and public-sale/tax controls as independent financial coverage.

P12 also deletes `compiler/execution.py::compile_holding_pools`' strategy-derived
declarations/first-source-account choice and
`product/scenarios.py::_target_allocation_policies_from_funding_policy` with their
last configured allocator consumers, along with `policy/configured_allocation.py`'s
configured-policy reader. The proposer is already Python-owned and calls shared
sleeve helpers; its language port is not remaining work. Declarations, not a strategy configuration,
must determine available accounts/instruments. Do not add new app endpoints or
financial features while retiring them.

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
