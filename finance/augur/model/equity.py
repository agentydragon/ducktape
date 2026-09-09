"""Equity identity and opening quote shared by replay and fitted market models.

Return dynamics and fitted parameters belong to the model generating the path.
This description does not specify dividend or tax treatment.
"""

from pydantic import PositiveFloat

from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import SecuritySymbol


class EquitySpec(FrozenModel):
    symbol: SecuritySymbol
    initial_price_usd: PositiveFloat
