# Optional policy helpers

Import <cash_band.py> for cash-budget proposals and <sleeves.py> for scoped public
portfolio trade proposals. A policy may compose or ignore them; they do not
execute actions, calculate taxes, advance time or read future paths.

Experiments own decision cadence, memory and ordered action lists. Financial
admission, accounting and settlement stay in the shared action executor; neutral
money/quantity conversions live in <../sim/fixed_point.py>.
