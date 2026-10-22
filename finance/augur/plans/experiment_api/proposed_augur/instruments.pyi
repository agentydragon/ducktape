"""Financial product identities and terms, shared across books, models and policies.

Distinguishes an actual fund, bond or home from a synthetic total-return index.
Model bindings supply prices/cashflows for these same products; they cannot change
their currency or contractual rights. Holdings and acquisition basis belong to
accounting, not product definitions. Concrete product schemas remain to be designed.
"""

from collections.abc import Mapping

type Weights = Mapping[Instrument, float]

class Instrument:
    name: str

class TotalReturnIndex(Instrument):
    def __init__(self, name: str, *, currency: str) -> None: ...

class Home(Instrument): ...
class ExecutionCosts: ...

class InvestableUniverse:
    execution_costs: ExecutionCosts
    def instrument(self, name: str) -> Instrument: ...
    def with_instrument(self, instrument: Instrument) -> InvestableUniverse: ...
