# The Trinity all-bond row is 43% against a published 80%

**Resolved: the cell has no resolution.** At a 3% withdrawal the 0%-equity row sits exactly
where the mass of 30-year windows crosses the survival boundary, so roughly one point of it
rides on one basis point a year of compound return. The apparent 27-point disagreement is
about a quarter point a year in the bond sleeve's level, amplified about a hundredfold by a
knife-edge statistic. Details in § What it actually is; the ruled-out sections below are kept
because two plausible stories died there and neither should be re-walked.

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

## What it actually is

Run the paper's own annual arithmetic — withdraw at the start of the year, earn that year's
return on the rest — over the same 32 windows (1927-1958 starts), on three return series:

| series                               | 3%  | 4%  | 5%  | 6%  |
| ------------------------------------ | --- | --- | --- | --- |
| SBBI Exhibit A-3 (what Trinity used) | 66% | 19% | 12% | 3%  |
| augur's sleeve, maturity 20          | 47% | 19% | 12% | 3%  |
| Table 3                              | 80% | 20% | 17% | 12% |

**At 4%, 5% and 6% the sleeve matches the real series exactly.** Those cells are not near the
boundary, so a level difference has nothing to flip. Only 3% moves, and it moves a lot: the
sleeve compounds about 0.2 points a year under SBBI in real terms (median window -0.54%
against -0.32%, worst -1.76% against -1.61%).

How much a level difference is worth there, adding a flat spread to the curve:

| spread | 3%  | 4%  | 5%  | 6%  | RMSE vs SBBI |
| ------ | --- | --- | --- | --- | ------------ |
| +0 bp  | 47% | 19% | 12% | 3%  | 2.81         |
| +10 bp | 56% | 22% | 12% | 3%  | 2.81         |
| +20 bp | 69% | 22% | 12% | 6%  | 2.81         |
| +40 bp | 72% | 25% | 12% | 9%  | 2.82         |

Twenty basis points a year buys 22 points of this cell and does not improve the sleeve's fit to
the actual return series at all. That is the whole phenomenon: about one point of "success
rate" per basis point per year, on 32 windows holding roughly two independent observations.

The residual after that — SBBI's own 66% against Table 3's 80%, on a window set friendlier than
the paper's — is 14 points, i.e. about 14 basis points a year. There is nothing left to explain.

## The one thing still worth fixing

The sleeve's 0.2 points a year is real and is not about this cell. The likely cause is the
yield source: Moody's Aaa is a narrower, higher-grade universe than the Aaa-and-Aa composite
Ibbotson priced, so its coupon is systematically light. Fix it against the return series, never
against Table 3 — a spread tuned to move this cell is the same mistake as maturity 10, one
level down.
