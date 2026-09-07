# The bond sleeve distributes more than it earns

`instrument_paths` (<../model/structural_macro.py>) pays a fund's coupon on a face pinned at
`initial_price_usd` for the whole horizon, while the mark moves with yields. Over a long
record the two drift apart and the payout stops being something the fund could fund.

Shared by both providers, so this is the fitted `structural_macro` as much as the historical
replay — a 30-year retirement projection is long enough for the drift to matter.

## Measured, 1926-07..1995-12, `BOND_SPEC` from <../study/trinity.py>

| quantity                                               | value                                          |
| ------------------------------------------------------ | ---------------------------------------------- |
| market yield for the instrument                        | 4.54% → 6.71%, mean 6.22%                      |
| **distribution yield on the mark**                     | **4.54% → 10.95%, mean 7.66%**                 |
| mark                                                   | 100.00 → 77.07                                 |
| total return                                           | 7.52%/yr — price leg −0.37%, income leg +7.92% |
| Cooley/Hubbard/Walz, long-term corporates, same period | 5.70%/yr                                       |

A fund holding bonds that yield 6.71% cannot pay 10.95% on its net asset value. The whole
1.8pp overshoot is in the income leg; the price leg contributes −0.37%/yr and is not the
problem. An earlier reading of this blamed the missing pull-to-par in the `exp(-D·Δy)` price
step — that is not it.

## Mechanism

`distribution = book_yield × initial_price_usd / 12`, so

    yield_on_mark[t] = book_yield[t] × initial_price_usd / price[t]

`book_yield` does converge to the market yield, as intended. But the constant numerator
against a drifting `price` leaves a permanent multiplier — 100/77.07 = 1.30 by 1995 — that
nothing corrects. Fall in the mark, and the fund's apparent yield rises without any bond in
it yielding more.

The docstring's rationale holds at the horizon it was written for: over months, a fund's face
per unit really is near-constant while the mark moves, which is the BND-2022 evidence it
cites. Over seventy years it does not — the fund rolls its portfolio many times, and each roll
buys with the market value it actually has, not with the value it started with.

## The invariant to restore

A fund's yield on its own mark should converge to the market yield, lagging by its turnover —
not drift away from it permanently. `book_yield` already carries that convergence, derived
from duration; face per unit needs to amortize toward the mark on the same schedule, since it
is the same roll.

Distributing on the _current_ mark instead is the opposite extreme — it assumes the whole
portfolio re-yields every month, discarding the book-yield lag the model exists to represent.
Measured, it gives 4.98%/yr against the paper's 5.70%: closer than today's 7.52%, still wrong,
and wrong in the other direction.

## Reproducing

`bbr run //finance/augur/study:trinity_bin` prints the whole-record compound return for both
sleeves against the paper's figures. The decomposition above came from calling
`instrument_paths` directly on `load_macro_history(...).restricted_to(...)` for the same span.
