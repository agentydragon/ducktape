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
    HomeValueKey,
    InflationKey,
    IssuerId,
    RentKey,
    SecurityKey,
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
    InflationKey | SecurityKey | HomeValueKey | RentKey | PrivateEquityMarkKey, Field(discriminator="kind")
]
"""A state-space covariance-basis factor: a non-PE level series this model fits, or a
private-equity issuer's mark.

**Listed explicitly, and deliberately not derived from the level-key union.** Deriving it looks
tidier and closes a real drift trap — a new `LevelSeriesKind` missing from a hand-copied list is
dropped from the basis with no type error — but it opens a worse one in the other direction: a
new emission kind would join this model's covariance basis by default, where `_coupling_allowed`
falls through to `return True` and gives it half its empirical correlation to every macro factor.
Silently gaining a factor is worse than silently missing one, because the missing one fails
loudly at use and the gained one just produces a slightly wrong answer forever.

The drift the list reintroduces is caught by `test_factor_key_covers_every_level_kind_deliberately`
instead, which fails on a new `LevelSeriesKind` and makes including it a decision someone records
rather than a default. What a model fits is that model's business; what the boundary emits is
everyone's. They are allowed to differ, and this is where they do."""


def parse_factor_key(wire_id: str) -> FactorKey:
    """Recover a typed `FactorKey` from its wire form — the state-space artifact's factor decode boundary.

    A level series this model fits decodes via `try_parse_level_series_key`; a
    `private_equity:{issuer}` wire id is a PE mark factor. Raises `ValueError` for
    anything else (the artifact would be carrying a factor the model can't produce).

    That last clause now includes a level series that IS emittable but is not in this model's
    basis: an emission kind added for some other provider is not automatically something this
    model knows how to fit, and an artifact naming one is wrong rather than novel.
    """

    if (level_key := try_parse_level_series_key(wire_id)) is not None:
        if not isinstance(level_key, InflationKey | SecurityKey | HomeValueKey | RentKey):
            raise ValueError(
                f"factor {wire_id!r} is a level series the state-space model does not fit; "
                "adding a level kind to its covariance basis is a modelling decision, not a default"
            )
        return level_key
    prefix, sep, suffix = wire_id.partition(":")
    if sep and prefix == "private_equity":
        return PrivateEquityMarkKey(issuer_id=IssuerId(suffix))
    raise ValueError(f"factor {wire_id!r} is neither a level series nor a private-equity mark")
