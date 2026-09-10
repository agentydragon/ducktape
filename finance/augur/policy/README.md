# Optional policy helpers

Import <cash_band.py> for cash-budget proposals and <sleeves.py> for scoped public
portfolio trade proposals. A policy may compose or ignore them; they do not
execute actions, calculate taxes, advance time or read future paths.

<funding.py> composes sales-only sleeve withdrawals with full due-claim payments
for a single cash account. Trinity and the bond-policy example share this policy;
surplus cash is never reinvested.

Experiments own decision cadence, memory and ordered action lists. Financial
admission, accounting and settlement stay in the shared action executor; neutral
money/quantity conversions live in <../sim/fixed_point.py>.
