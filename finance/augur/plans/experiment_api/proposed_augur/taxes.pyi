"""Versioned tax rules and accumulated filing-unit state.

Computes tax consequences from transaction facts and prior income, gains, losses
and payments. Owns statutory classification and cross-transaction interactions,
not market forecasts or discretionary liquidation choices. Rules must declare
supported jurisdictions/products and reject relevant unsupported cases; NoTax is
an explicit study assumption, never a fallback for missing tax support.
"""

from collections.abc import Sequence
from datetime import date

import polars as pl
from proposed_augur.accounting import FinancialEvent

class TaxState:
    """Filing units, residency, year-to-date facts, carryovers, liabilities and payments."""

class TaxAssessment:
    """Updated tax state plus liabilities and payments due, not cash already paid."""

    state: TaxState
    liabilities: pl.DataFrame
    payments_due: pl.DataFrame

class TaxRules:
    """Supplied law versions and explicit future-law/indexation assumptions."""

    def assess(self, state: TaxState, events: Sequence[FinancialEvent], *, at: date) -> TaxAssessment:
        """Calculate incremental consequences in full filing-unit context, without posting cashflows."""

class NoTax(TaxRules): ...
