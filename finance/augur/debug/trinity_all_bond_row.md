# The Trinity all-bond row is 43% against a published 80%

**Partly resolved.** Of the 33 points between this sleeve and Table 3 on a consistent basis,
19 are the bond series — a quarter point a year of compound return, amplified by a cell that
sits on the survival boundary — and **14 remain unexplained**. Details in § What it actually
is. The ruled-out sections below are kept because two plausible stories died there
(funds-versus-ladders, then duration) and neither should be re-walked.

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

### Why 3% and not the others

A 30-year CPI-indexed withdrawal survives iff the window's real return clears a break-even
fixed by the rate. A cell is sensitive exactly insofar as window mass sits near its own
break-even, and that is the whole story of which cells move:

| rate | break-even real return | median window minus break-even | windows within ±0.25pp |
| ---- | ---------------------- | ------------------------------ | ---------------------- |
| 3%   | −0.71%                 | +0.39pp                        | 7 of 32                |
| 4%   | +1.31%                 | −1.63pp                        | 2 of 32                |
| 5%   | +3.08%                 | −3.40pp                        | 0 of 32                |
| 6%   | +4.70%                 | −5.02pp                        | 0 of 32                |

The median window clears the 3% break-even by 0.39pp — less than half a point from a coin
flip — while 5% and 6% are three to five points away from anything. That is why a 0.20pp
sleeve difference is worth 19 points at 3% and zero at 5%. It is forced by where the
break-evens fall, not a coincidence.

## What is still open

SBBI's own 66% against Table 3's 80% — 14 points, about 4.5 windows of 32 — is not explained
by anything here, and the window set makes it worse rather than better: these 32 start
1927-1958, where SBBI's published returns end, while the paper's 41 run to a 1965 start. Those
later starts eat the 1965-82 inflation early and are the worst in the record, so on the paper's
own windows the real series would land below 66%, widening the gap.

Leads, in order of promise:

- **The terminal-year success rule.** Table 3's all-bond cells drop 60 points between 3% and
  4%; ours drop 28. A partially-funded final withdrawal counted as success there and failure
  here would land almost entirely on the marginal cell, which is exactly where the gap is.
- The paper used annual SBBI data; the 1998 article's method section does not pin whether the
  withdrawal precedes or follows the year's return, and its one falsifiable datum (a 1929
  15-year failure) does not discriminate.
- Whether Table 3's all-bond row is reproducible from Ibbotson's data at all. Every other row
  family reproduces.

## The one thing still worth fixing

The sleeve's 0.2 points a year is real and is not about this cell. The likely cause is the
yield source: Moody's Aaa is a narrower, higher-grade universe than the Aaa-and-Aa composite
Ibbotson priced, so its coupon is systematically light. Fix it against the return series, never
against Table 3 — a spread tuned to move this cell is the same mistake as maturity 10, one
level down.
