# The Trinity all-bond row is 43% against a published 80%

Open. The equity-holding rows of `study/trinity` land within about a point of Table 3 over
3-4%; the 0%-equity row does not, and nothing named in `replay.py`'s attribution accounts for
it. This records what has been ruled out, so the next attempt does not re-walk it.

## Ruled out: the fund is a fund and Trinity priced a ladder

This was the original diagnosis and it is **wrong**. Trinity's bonds are Ibbotson's long-term
high-grade corporate series, and Ibbotson & Sinquefield document its construction (SBBI
1926-1987, "Description of the Basic Series", p. 21):

> Monthly capital appreciation returns for 1926-68 were calculated from yields assuming (at
> the beginning of each monthly holding period) a 20-year maturity, a bond price equal to par,
> and a coupon equal to the yield. [...] The monthly income return is assumed to be
> one-twelfth of the coupon.

That is `constant_maturity_fund_paths` clause for clause. From 1969 the series splices in the
Salomon Brothers Long-Term High-Grade Corporate Bond Index, "nearly all Aaa- and Aa-rated
bonds" — a marked index, also never held to maturity. Trinity's bonds do not pull to par and
do not repay principal on a date. There is no ladder anywhere in the comparison.

## Ruled out: the maturity

Fitting maturity against that series' published annual returns (SBBI Exhibit A-3, whose
diagonal gives the annual total returns; the monograph's own stated extremes — 43.79% in 1982,
-8.09% in 1969 — confirm the extraction) using Moody's Aaa as the yield input:

| maturity | RMSE 1946-68 (documented: 20) | RMSE 1969-85 (the real index) |
| -------- | ----------------------------- | ----------------------------- |
| 5        | 3.40                          | 8.07                          |
| 10       | 2.54                          | 5.67                          |
| 15       | 2.04                          | 4.57                          |
| 20       | **1.88**                      | 4.15                          |
| 25       | 1.95                          | **4.04**                      |

The 1946-68 column is a control with a documented answer, and it recovers 20 exactly, which is
what makes the 1969-85 column worth reading. That one rejects anything short: the era's call
and sinking-fund provisions do not show up as a shorter effective maturity. At 20 the sleeve
compounds 4.99% against SBBI's 5.17% over 1927-85, standard deviation 7.9% against 8.8%, mean
signed error -0.23 points a year in the first era and -0.39 in the second.

**Maturity 10 lands the all-bond row on Table 3 almost exactly (81% against 80%) and is
wrong** — it misses the actual return series by half again as much as 20 does, in both eras.
That coincidence is the trap this table exists to document.

## Ruled out: the conventions

Trinity's own arithmetic on the sampled paths, outside the simulator, maturity 20, 3%
withdrawal, all-bond (Table 3: 80%):

| coupon     | start-of-year | end-of-year |
| ---------- | ------------- | ----------- |
| idle cash  | 42%           | 38%         |
| reinvested | 53%           | 51%         |

Reinvestment is worth 11 points, timing about 2, monthly rather than annual window starts
about 3 (53% against 50% on the paper's 41 annual starts). The simulator agrees with the
hand arithmetic — augur's 43% against this 42% — so the sim is not the suspect either.

Roughly 27 points remain, and they are not in: the instrument, the maturity, the coupon
convention, the withdrawal timing, the window spacing, or the simulator.

## Where to look next

- The paper's success criterion for the terminal year. A 60-point drop between its 3% and 4%
  all-bond cells is a cliff; ours drops 37. If a partially-funded final withdrawal counts as
  success there and as failure here, that lands disproportionately on the marginal cell.
- Annual versus monthly compounding of the withdrawal against the return.
- Whether Table 3's all-bond row is reproducible from Ibbotson's data at all. The equity rows
  reproduce; this one is the only cell family that does not, and the paper is a 6-page journal
  article whose method section does not pin the arithmetic.
