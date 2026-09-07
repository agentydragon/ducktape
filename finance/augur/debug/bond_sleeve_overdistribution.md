# The bond sleeve distributed more than it earned

Resolved. Kept as the record of a defect that a green internal test suite could not see, and
of the check that would have caught it years earlier.

## What was wrong

`instrument_paths` paid a fund's coupon on a face pinned at `initial_price_usd` for the whole
horizon while the mark moved with yields, so

    yield_on_mark[t] = book_yield[t] × initial_price_usd / price[t]

drifted away from the market yield without limit. Measured over 1926-07..1995-12 on a
12-year-duration corporate sleeve: the fund's distribution yield on its own net assets reached
**10.95% while the bonds it held yielded 6.71%**, and its total return was **7.52%/yr against
the 5.70%** Cooley/Hubbard/Walz report for long-term corporates over the same period. The mark
ended at 77.07, so the constant numerator was inflating the payout by 1.30x.

The whole error was in the income leg (+7.92%/yr); the price leg contributed −0.37%/yr. An
earlier reading blamed the missing pull-to-par in the `exp(-D·Δy)` price step — that was wrong.

## Why it survived

The function glued together three different instruments. The price response was a
constant-maturity roll; the `book_yield` convergence, with a half-life of the fund's duration,
described a LADDER of staggered maturities; and the fixed face belonged to neither. Each piece
had a defensible story — the docstring's BND-2022 evidence for the payout lag is real — and no
test compared the assembly against anything outside augur.

## What replaced it

<../model/bond_fund.py>: a constant-maturity fund holding a par bond, collecting its coupon,
and rolling into a fresh one each month, buying the face its net asset value affords. Exact
present-value pricing, so duration is an output rather than a parameter, and the payout is
struck on the previous mark, so yield-on-mark is the yield of the bonds held with no drift
term. One period of it reproduces Damodaran's published annual Baa returns to 1e-12.

Corporate sleeves now price off Moody's Aaa/Baa (FRED, monthly, 1919-) rather than a
government yield plus a guessed constant spread.

## The check that caught it, and the one that would have caught it sooner

An **external** number. Every prior check on this code was internal — the engines agreed with
each other, the money math was exact — and none of that can catch a model that is
self-consistently wrong. Anchoring on the INPUTS against a published figure is what turned
"the bond sleeve looks a bit generous" into a located defect.

Second moments matter as much as first. Against Ibbotson's ~5.7%/yr at ~8.5% sd for long-term
corporates over 1926-1995, the replacement on Moody's Aaa realizes:

| maturity | CAGR  | annual sd |
| -------- | ----- | --------- |
| 10       | 5.79% | 6.66%     |
| 20       | 5.65% | 8.85%     |
| 25       | 5.60% | 9.48%     |

A check on the mean alone would have accepted the 10-year fund, whose volatility is a third
too low — and volatility is what a withdrawal study's tails are made of.
