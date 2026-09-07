# augur/study/sbbi

Ibbotson and Sinquefield, _Stocks, Bonds, Bills, and Inflation: Historical Returns
(1926-1987)_, CFA Research Foundation, 1989 — the source of the long-term corporate bond
series the Trinity study's bonds are.

`long_term_corporate.py` prices Moody's Aaa through `bond_fund.constant_maturity_fund_paths` at
the 20-year maturity the monograph documents, and compares the result to its published annual
returns. Its docstring carries the attribution: the construction is quoted from the monograph
rather than chosen, and the one difference — the yield universe — is a known defect with no
public fix.

This is the external check on augur's bond arithmetic. `bond_fund`'s own tests pin a
one-period identity against Damodaran and a handful of internal invariants; only this one runs
the construction over a real century against somebody else's published result.

`manual` (see the parent README) — the Aaa yields are fetched from FRED:

```bash
bbr test //finance/augur/study/sbbi:long_term_corporate_test
bbr run //finance/augur/study/sbbi:long_term_corporate_bin   # the maturity fit, printed
```
