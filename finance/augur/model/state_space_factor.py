"""Typed factor identity for the state-space trained model's covariance basis.

The state-space exogenous model fits a fixed, ordered set of *factors* as one
joint monthly-log-return distribution (mean + block-shrunk covariance). Unlike
every other layer in augur — which speaks `LevelSeriesKey` — this basis is the
union of two disjoint kinds, because state-space is the one model that fits
public level series and private-equity issuer marks *jointly* in a single
covariance:

  - a non-PE level series (`LevelSeriesKey`), or
  - a private-equity issuer's per-unit mark series (`PrivateEquityMarkKey`).

`FactorKey` is that union; `parse_factor_key` decodes the artifact's on-disk
wire-id strings back to it. This type is intentionally *not* part of the shared
model/sim vocabulary — the sampler routes level factors onto their
`LevelSeriesKey` and PE-mark factors into the typed `PrivateEquityBundle`, so
nothing outside the state-space artifact needs it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import (
    CryptoKey,
    HomeValueKey,
    InflationKey,
    IssuerId,
    RentKey,
    SP500Key,
    try_parse_level_series_key,
)


class PrivateEquityMarkKey(FrozenModel):
    """A state-space factor that is a private-equity issuer's per-unit mark series.

    Not a level series — PE marks are carried in the typed `PrivateEquityBundle` at
    sample time — but it *is* a factor in the state-space covariance basis, which spans
    both non-PE level series and PE marks. `kind` is the discriminator; the wire form
    matches the `private_equity:{issuer}` prefix the artifacts have always used.
    """

    kind: Literal["private_equity_mark"] = "private_equity_mark"
    issuer_id: IssuerId

    @property
    def wire_id(self) -> str:
        return f"private_equity:{self.issuer_id}"


type FactorKey = Annotated[
    InflationKey | SP500Key | HomeValueKey | RentKey | CryptoKey | PrivateEquityMarkKey, Field(discriminator="kind")
]
"""A state-space covariance-basis factor: any non-PE level series, or a private-equity issuer's mark."""


def parse_factor_key(wire_id: str) -> FactorKey:
    """Recover a typed `FactorKey` from its wire form — the state-space artifact's factor decode boundary.

    A non-PE level series decodes via `try_parse_level_series_key`; a
    `private_equity:{issuer}` wire id is a PE mark factor. Raises `ValueError` for
    anything else (the artifact would be carrying a factor the model can't produce).
    """

    if (level_key := try_parse_level_series_key(wire_id)) is not None:
        return level_key
    prefix, sep, suffix = wire_id.partition(":")
    if sep and prefix == "private_equity":
        return PrivateEquityMarkKey(issuer_id=IssuerId(suffix))
    raise ValueError(f"factor {wire_id!r} is neither a level series nor a private-equity mark")
