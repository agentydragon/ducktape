"""Nominal identities of the entities a world declares and addresses.

Each is a plain `str` at runtime and on the wire; the distinct types keep one kind of
identity from standing in for another under type checking.
"""

from typing import NewType

AgentId = NewType("AgentId", str)
AccountId = NewType("AccountId", str)
# A public security's symbol, or `private_equity:<issuer>` for a private holding (`private_issuer`).
AssetId = NewType("AssetId", str)
LotId = NewType("LotId", str)
PortfolioId = NewType("PortfolioId", str)
BondId = NewType("BondId", str)
PropertyId = NewType("PropertyId", str)
# A mortgage: the liability a financed purchase originates.
LiabilityId = NewType("LiabilityId", str)
JurisdictionId = NewType("JurisdictionId", str)
