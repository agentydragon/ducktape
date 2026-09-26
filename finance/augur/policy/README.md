# Optional policy helpers

Import <cash_band.py> for cash-budget proposals and <sleeves.py> for scoped public
portfolio trade proposals. A policy may compose or ignore them; they do not
execute actions, calculate taxes, advance time or read future paths.

<funding.py> composes sales-only sleeve withdrawals with full due-claim payments
for a single cash account. Trinity and the bond-policy example share this policy;
surplus cash is never reinvested. Its `ClaimPayer` pays claims and never trades.

<cash_band_household.py> is a household agent holding one cash account in a band:
target-weighted sleeve sales (a public security's lots, or a managed portfolio's
money), full due-claim payments, then optional surplus and drift purchases. The app
builds one from its funding policy.

Sleeve `withdraw` weights account/asset pools independently; `withdraw_by_symbol`
weights each symbol once across explicitly ordered accounts, using FIFO within
each account. Both reuse the same allocation and exact-lot proposal calculations.
Zero targets stay sellable at this level; product-specific exclusions belong to
the calling policy.

Experiments own decision cadence, memory and ordered action lists. Financial
admission, accounting and settlement stay in the shared action executor; neutral
money/quantity conversions live in <../sim/fixed_point.py>.
