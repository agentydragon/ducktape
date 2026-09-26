# P12: configured-strategy readers

The [roadmap](roadmap.md) owns P12's dependencies and dispatch. These are the
configured strategy's live readers and their deletion criteria, not a new framework.
Remove each entry with its last reader. No new configured implicit strategies or
experiment consumers are added meanwhile.

## Readers

- The PE issuer phase selects recovery, forced and tender lots with `Holdings.fifo`
  inside the world; PE's migration after GPE replaces that with explicit responses. Do
  not promote its mixed-scale raw-quantity selection to a generic sleeve helper.

## Gaps on the app's funding path

- The app's household never reinvests (`reinvest=None`), so the app never buys or
  contributes, and the zero-mark contribution refusal
  (`TlhPortfolioObservation.accepts_contributions`) is reachable only from household
  tests (`policy/test_cash_band_household{,_world}.py`).

## Older PR disposition

A proposed disposition, not a claim that the PR is closed. Remove the row when it is
resolved.

| PR                                                          | Disposition and surviving requirement                                                                                                                                                                                                                                        |
| ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [#5859](https://github.com/agentydragon/ducktape/pull/5859) | Keep as review-only study input, not another supported interface package. Consume its studies through STUDY/RUN/HOUSE/SCORE/ROBUST; inner-forecast continuation remains future scope. Replace sketches with runnable consumers rather than implementing every proposed stub. |

Optional model research lives in [the research note](market_model_research.md);
implemented behavior lives in [calibration](../docs/calibration.md) and
[PE model](../docs/private_equity_model.md) documentation.
