# CPI-YoY Kalshi calibration market line — spikiness RCA + fix

## Symptom

The calibration "market line" for the CPI-YoY-on-Kalshi threshold ladder
(`cpi_yoy_july_2026`, 21 rungs 3.0%–5.0%) rendered spiky/weird.

## Root cause (from live Kalshi data, not the hypothesis)

The pipeline fed each rung's `last_price_dollars` into the survival curve. On this thin ladder
`last_price` is unreliable:

1. **`last_price` is set by sub-$1 fractional fills.** Rung T4.1 reported `last=0.04` from two
   0.99- and 0.01-contract trades, while real size (484+200 contracts) had traded at 0.58–0.63 and
   the live book was 0.69–0.84. Differencing that stale/tiny value produced the dominant ~0.42–0.51
   bucket spike.
2. **Untraded rungs report `last_price="0.0000"` (a string, not JSON `null`).** The old
   `last_price_dollars: float | None` guard and the `_live` drop-on-`None` never fired, so untraded
   rungs (T3.1, T3.5) injected a fake `P=0` survival point. Only 2 of 21 rungs are truly untraded —
   most of the visible zeros were PAVA-pooling/differencing artifacts.
3. **The tail (4.2%–5.0%) has genuinely wide / one-sided books** (e.g. T4.2 `bid=0.02 / ask=0.82`),
   so any single-snapshot estimate there is low-confidence. `volume_24h=0` on nearly every rung —
   the order-book mid is the only always-fresh signal.

The bucket families (SP500/BTC/ETH, all liquid) and the IPO date ladder were already healthy; CPI
is sick purely because it is thin.

## Fix

Principled two-layer conversion (see `calibration/quote.py`, `calibration/calibration.py`):

- **Layer 1 (market → probability):** Stoikov micro-price (size-weighted mid) when a genuine
  two-sided book exists, degenerating to the plain midpoint without sizes; volume-backed `last`
  only as a one-sided fallback; untraded/empty → no observation (never `0`/`0.5`).
- **Layer 2 (ladder → distribution):** confidence-weighted isotonic regression (weighted PAVA,
  weight = inverse-variance from spread + depth) + adjacent differencing (discrete
  Breeden–Litzenberger). Unpriced rungs are interpolated across, not dropped or zeroed.

## Before/after on live data

From `bb run //finance/augur/calibration/x:inspect_cpi_quotes` (exercises the real pipeline):

```
OLD buckets (last-trade, uniform):  [0.01, 0,0,0,0, 0.03, 0.03, 0, 0.06, 0.055, 0, 0.51, 0, 0.015, 0, 0.08, 0.13, 0,0,0,0, 0.08]
NEW buckets (quote-mid, weighted):  [0.287, 0,0,0,0,0,0, 0.015, 0, 0.053, 0,0, 0.313, 0,0,0,0, 0.044, 0,0,0, 0.288]
```

The dominant spurious `0.51` spike (T4.1 fractional-fill last trade) is gone; mass is driven by the
confident rungs. Residual lumpiness reflects the ladder's genuine wide-book uncertainty, not a code
artifact. Untraded T3.1/T3.5 are dropped from the fit and interpolated.

Codified hermetically in `test_calibration.py::test_threshold_ladder_uses_quote_mids_and_interpolates_unpriced`
and the Layer-1 unit tests in `test_quote.py`.
