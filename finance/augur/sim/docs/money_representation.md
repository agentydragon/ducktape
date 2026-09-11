# Exact money representation

Runtime money is an integer count of the scenario's declared currency quantum,
not a floating-point currency amount. The authoring quantum is an exact Decimal
and need not be a power of ten: five-rappen and fifth-unit currencies remain valid.
Public money counts preserve their declared signed range; Python's arbitrary-size
integers permit safe intermediate products, not silent widening of that contract.

<../money.py> owns checked addition, subtraction, ratios, lot-basis apportionment,
and **nearest-integer rounding with exact ties away from zero**. Python `round`
(banker's ties) and negative floor division are not substitutes for those rules.
Quantities retain explicit power-of-ten scales. Partial disposal apportions basis;
final liquidation consumes the exact remaining basis instead of accumulating
per-fill rounding drift.

<../test_money.py> includes generated Hypothesis properties for small/large and
negative denominators, constructed exact ties, range boundaries, and complete
lot drawdowns. The primary rounding oracle multiplies back and checks nearest
distance and tie direction rather than copying the implementation's division.
Ledger and transaction controls additionally verify balanced postings and rejected
compound updates leave every affected financial book unchanged.
