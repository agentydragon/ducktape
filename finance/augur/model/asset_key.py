"""Asset identity shared by market models, scenario holdings and result consumers.

Security keys identify price series; private-equity keys identify issuers in a
typed bundle. Frame/wire boundaries serialize these keys through `wire_id` and
`parse_asset_key`; identity does not depend on the app or an execution strategy.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import AssetPriceKey, IssuerId, SecurityKey, SecuritySymbol


class AssetKind(StrEnum):
    """Discriminator value for the one asset kind that is NOT a level series.

    The tradable kind reuses `LevelSeriesKind` — an `AssetKey` for a security IS the
    `AssetPriceKey` that prices it, so there is nothing to discriminate differently.
    """

    PRIVATE_EQUITY = "private_equity"


class PrivateEquityAssetKey(FrozenModel):
    """A private-equity holding. Priced by the typed `PrivateEquityBundle`, not a level series.

    The only asset kind with no asset-price key, which is exactly why `AssetKey` is a wider
    union than `AssetPriceKey` rather than a parallel vocabulary.
    """

    kind: Literal[AssetKind.PRIVATE_EQUITY] = AssetKind.PRIVATE_EQUITY
    issuer_id: IssuerId

    @property
    def wire_id(self) -> str:
        return f"private_equity:{self.issuer_id}"


type AssetKey = Annotated[SecurityKey | PrivateEquityAssetKey, Field(discriminator="kind")]
"""What a lot/sale identifies: a tradable asset-price series, or a private-equity holding.

An INCLUSION, not a parallel hierarchy: `AssetKey` is `AssetPriceKey` plus private equity.
The tradable member is literally the model's `SecurityKey`, so the conversion that used to
sit between the two vocabularies is now just the PE narrowing.
"""


def parse_asset_key(wire_id: str) -> AssetKey:
    """Recover a typed `AssetKey` from its wire form. Raises `ValueError` if unrecognized."""

    prefix, sep, suffix = wire_id.partition(":")
    if not sep:
        raise ValueError(f"unrecognized asset wire id {wire_id!r}")
    match prefix:
        case "security":
            return SecurityKey(symbol=SecuritySymbol(suffix))
        case "private_equity":
            return PrivateEquityAssetKey(issuer_id=IssuerId(suffix))
    raise ValueError(f"unrecognized asset wire id {wire_id!r}")


def asset_price_key(asset: AssetKey) -> AssetPriceKey:
    """Narrow a lot/sale identifier to the asset-price series that prices it.

    Private equity is priced by its own typed `PrivateEquityBundle`, not a level series, so
    it has no asset-price key and raises. Every other `AssetKey` already IS one.
    """

    if isinstance(asset, PrivateEquityAssetKey):
        raise ValueError(f"private-equity asset {asset!r} has no asset-price series key")
    return asset


def asset_price_key_or_none(asset: AssetKey) -> AssetPriceKey | None:
    """`asset_price_key` for tradable assets; `None` for private equity (priced off-series).

    For compile sites that range over mixed lots/chains and must skip PE.
    """

    return None if isinstance(asset, PrivateEquityAssetKey) else asset
