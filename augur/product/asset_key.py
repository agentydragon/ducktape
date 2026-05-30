"""Typed identifiers for sim/product asset references.

`Lot.asset_id`, the `asset_id` column on sim event frames, and the
`asset_id` field on product wire events used to encode the asset's kind in a
magic prefix (`"private_equity:..."`, `"crypto:..."`) that Python dispatch
sites then matched with `.startswith(...)`. That dispatch is now typed: an
`AssetKey` is a Pydantic discriminated union with a `StrEnum` `kind`
discriminator, recovered from the wire string by `parse_asset_key`.

The wire string format is preserved for serialization (JSON, polars
columns, fixture YAML). Producers obtain it via `AssetKey.wire_id`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from augur.model.schemas import FrozenModel
from augur.model.series import CryptoSymbol, IssuerId


class AssetKind(StrEnum):
    """Discriminator for `AssetKey` variants.

    `StrEnum` (not `IntEnum`) so the discriminator renders as a human-readable
    `kind: crypto` wherever an `AssetKey` is Pydantic-serialized (portfolio
    `value_series`, API wire). Pure discriminator — wire ids come from each
    variant's `wire_id`, never from this enum.
    """

    SP500 = "sp500"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"


class _AssetKeyBase(FrozenModel):
    @property
    def wire_id(self) -> str:
        raise NotImplementedError


class SP500AssetKey(_AssetKeyBase):
    kind: Literal[AssetKind.SP500] = AssetKind.SP500

    @property
    def wire_id(self) -> str:
        return "sp500"


class CryptoAssetKey(_AssetKeyBase):
    kind: Literal[AssetKind.CRYPTO] = AssetKind.CRYPTO
    symbol: CryptoSymbol

    @property
    def wire_id(self) -> str:
        return f"crypto:{self.symbol}"


class PrivateEquityAssetKey(_AssetKeyBase):
    kind: Literal[AssetKind.PRIVATE_EQUITY] = AssetKind.PRIVATE_EQUITY
    issuer_id: IssuerId

    @property
    def wire_id(self) -> str:
        return f"private_equity:{self.issuer_id}"


type AssetKey = Annotated[SP500AssetKey | CryptoAssetKey | PrivateEquityAssetKey, Field(discriminator="kind")]


def parse_asset_key(wire_id: str) -> AssetKey:
    """Recover a typed `AssetKey` from its wire form. Raises `ValueError` if unrecognized."""

    if wire_id == "sp500":
        return SP500AssetKey()
    prefix, sep, suffix = wire_id.partition(":")
    if not sep:
        raise ValueError(f"unrecognized asset wire id {wire_id!r}")
    match prefix:
        case "crypto":
            return CryptoAssetKey(symbol=CryptoSymbol(suffix))
        case "private_equity":
            return PrivateEquityAssetKey(issuer_id=IssuerId(suffix))
    raise ValueError(f"unrecognized asset wire id {wire_id!r}")


def try_parse_asset_key(wire_id: str) -> AssetKey | None:
    """Return a typed key or `None` for unrecognized asset wire ids."""

    try:
        return parse_asset_key(wire_id)
    except ValueError:
        return None
