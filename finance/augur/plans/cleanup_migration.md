# P12: configured-strategy readers

The [roadmap](roadmap.md) owns P12's dependencies and dispatch. These are the
configured strategy's live readers and their deletion criteria, not a new framework.
Remove each entry with its last reader. No new configured implicit strategies or
experiment consumers are added meanwhile.

## Readers

- `World.declare_housing` (`sim/property.py::Housing`): scripted purchases, sales,
  residence and rented-share changes and improvements the world executes on schedule,
  which the app declares from its request. HOUSING's migration after GHOUSE replaces
  them with household actions.
- `World.declare_tender_policy` (`sim/prepared.py::_TenderPolicy`): a liquid-net-worth
  floor the world sells to on the owner's behalf; without one, compulsory recovery is
  skipped. The PE issuer phase selects recovery, forced and tender lots with
  `Holdings.fifo` inside the world; PE's migration after GPE replaces both with
  explicit responses. Do not promote its mixed-scale raw-quantity selection to a
  generic sleeve helper.

## Gaps on the app's funding path

- The app's household never reinvests (`reinvest=None`), so the app never buys or
  contributes, and the zero-mark contribution refusal
  (`TlhPortfolioObservation.accepts_contributions`) is reachable only from household
  tests (`policy/test_cash_band_household{,_world}.py`). Turning it on (a
  `FundingPolicy.reinvest_surplus` flag, off by default, passed through
  `_funding_household` as `Reinvest(rebalance_tolerance_ppb=None)`, plus a funding-form
  checkbox, `SCENARIO_SET_VERSION` bump and dropping "nothing buys" from `FundingPolicy`'s
  docstring) still lacks:
  - A security purchase in the timeline. `Holdings.buy` records no acquisition and no
    event frame carries one, so a `Buy` moves the cash and holding-value series but
    renders nothing. Needs an acquisition record captured into a new `EventLog` frame,
    a `HoldingPurchaseEvent` in `product/wire.py` and `ROLLOUT_EVENT_KIND_ORDER`, and its
    frontend rendering. Contributions already render as `tlh_financial_effect` rows.
  - A purchase pool per security sleeve. Purchases land in `source_account_ids[0]`, and
    `CashBandHousehold.check` refuses a sleeve without a declared pool there; the app
    declares pools only from lots (`compile_holding_pools`), so a sleeve held only in a
    later account fails. Declare an empty pool for each targeted security in that
    account, or choose a per-sleeve purchase account.

## Older PR disposition

A proposed disposition, not a claim that the PR is closed. Remove the row when it is
resolved.

| PR                                                          | Disposition and surviving requirement                                                                                                                                                                                                                                        |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#5859](https://github.com/agentydragon/ducktape/pull/5859) | Keep as review-only study input, not another supported interface package. Consume its studies through STUDY/RUN/HOUSE/SCORE/ROBUST; inner-forecast continuation remains future scope. Replace sketches with runnable consumers rather than implementing every proposed stub. |

Optional model research lives in [the research note](market_model_research.md);
implemented behavior lives in [calibration](../docs/calibration.md) and
[PE model](../docs/private_equity_model.md) documentation.
