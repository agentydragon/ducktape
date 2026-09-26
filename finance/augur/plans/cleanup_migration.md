# P12: configured-strategy readers

The [roadmap](roadmap.md) owns P12's dependencies and dispatch. These are the
configured strategy's live readers and their deletion criteria, not a new framework.
Remove each entry with its last reader. No new configured implicit strategies or
experiment consumers are added meanwhile.

## Readers

- `policy/configured_household.py::ConfiguredHousehold`, the `_AllocationPolicy`
  records it adapts (`sim/prepared.py`) and the sim's `TargetAllocationPolicy` schema:
  the sim suites move onto `policy/cash_band_household.py` (or `ClaimPayer`), and the
  records go with the household. Keep the shared sleeve calculations
  (`policy/{cash_band,sleeves}.py`); their tests cover exact allocation, reserved
  cash, zero targets/full exits, FIFO scoping and quantity scales. A newly required
  product-specific calculation needs a real Python consumer and independent financial
  checks; do not promote mixed-scale raw-quantity PE selection to a generic helper.
- Scheduled sales: `_ScheduledSale` (`sim/prepared.py`) is an input only the sim and
  product suites build; the configured household turns it into FIFO `Sell`s. Those
  suites move their sales to explicit actions, and the record goes with the household.
  Explicit asset-sale and public-sale/tax controls remain the independent coverage.
- The PE issuer phase selects recovery, forced and tender lots with `Holdings.fifo`
  inside the world; PE's migration after GPE replaces that with explicit responses.

## Gaps on the app's configured path

- The app's household never reinvests (`reinvest=None`), so the app never buys or
  contributes, and the zero-mark contribution refusal
  (`TlhPortfolioObservation.accepts_contributions`) is reachable only from household
  tests (`policy/test_cash_band_household.py`, `policy/test_configured_household.py`).

## Older PR disposition

A proposed disposition, not a claim that the PR is closed. Remove the row when it is
resolved.

| PR                                                          | Disposition and surviving requirement                                                                                                                                                                                                                                        |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#5859](https://github.com/agentydragon/ducktape/pull/5859) | Keep as review-only study input, not another supported interface package. Consume its studies through STUDY/RUN/HOUSE/SCORE/ROBUST; inner-forecast continuation remains future scope. Replace sketches with runnable consumers rather than implementing every proposed stub. |

Optional model research lives in [the research note](market_model_research.md);
implemented behavior lives in [calibration](../docs/calibration.md) and
[PE model](../docs/private_equity_model.md) documentation.
