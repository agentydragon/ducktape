"""Which model produced a sampled bundle, and what that model rests on.

A probability out of Augur is a claim under the model that produced it (<../AGENTS.md>
§ Provenance of a reported number). `ModelIdentity` is that model as its sampler reports it:
the family, the fitted artifact it drew with and the evidence window of each fitted block, and
the seeds or historical windows its rollouts came from. A fact the sampler cannot supply is a
named variant (`NoArtifact`, `WindowNotRecorded`), so its absence reads as absent rather than
as an empty default.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from finance.augur.model.schemas import FrozenModel


def stable_identity_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(_json_stable(payload), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def _json_stable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_stable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_stable(inner) for key, inner in value.items()}
    if isinstance(value, tuple | list):
        return [_json_stable(inner) for inner in value]
    return value


class FitWindow(FrozenModel):
    """A fitted block's evidence window, checked in as data rather than left to a comment
    beside the numbers: which series it came from, the window it was fitted on, and how many
    months that window covered."""

    source: str
    first_month: date
    last_month: date
    sample_months: int


class ModelKind(StrEnum):
    """The sampler family behind a bundle, named as its provider config `type` where it has one."""

    STRUCTURAL_MACRO = "structural_macro"
    VECM = "vecm"
    STATE_SPACE = "state_space"
    INDEPENDENT = "independent"
    TRAINED_PRIVATE_EQUITY = "trained_private_equity"
    PRIVATE_EQUITY_RISK = "private_equity_risk"
    HISTORICAL_WINDOWS = "historical_windows"
    COMPOSITE = "composite"
    MIRRORING = "mirroring"
    STIPULATED = "stipulated"
    """A test fixture: the caller wrote the paths down, and no model stands behind them."""


@dataclass(frozen=True)
class WindowNotRecorded:
    """The artifact does not record, as typed data, the months this block was fitted on."""


@dataclass(frozen=True)
class FittedComponent:
    """One separately fitted block of an artifact: a joint VAR, an equity marginal, one issuer."""

    name: str
    window: FitWindow | WindowNotRecorded


@dataclass(frozen=True)
class FittedArtifact:
    """Parameters estimated from evidence, one entry per fitted block the sampler draws with.

    `digest` hashes the fitted values actually drawn with, so it moves when they do (a refit, or
    a block overridden out of the fit) and with nothing else: not the seeds, the horizon, a
    deployment's opening anchors or the instruments priced off the paths. Parameters stated
    beside the fitted ones, such as a coupling zeroed by policy, are outside it too.
    """

    digest: str
    components: tuple[FittedComponent, ...]

    def __post_init__(self) -> None:
        if not self.components:
            raise ValueError("a fitted artifact names at least one fitted block")


@dataclass(frozen=True)
class NoArtifact:
    """Nothing was fitted: every parameter is stated in configuration."""


class SeedDerivation(StrEnum):
    """How a rollout's seed becomes its random draws."""

    PER_ROLLOUT = "per_rollout"
    """A rollout's paths depend on its own seed alone, so it re-runs by itself and does not
    change with the batch it was sampled in."""

    BATCH_MIXED = "batch_mixed"
    """Each stream draws from one generator seeded by every seed in the batch, so a rollout's
    path changes when the rest of its batch does."""


@dataclass(frozen=True)
class Drawn:
    """Rollouts drawn at random from a parametric model."""

    artifact: FittedArtifact | NoArtifact
    rollout_seeds: tuple[int, ...]
    seed_derivation: SeedDerivation


@dataclass(frozen=True)
class Replayed:
    """Rollouts cut from an observed record, one window per rollout; nothing is drawn.

    Consecutive windows overlap, so the rollouts are not independent observations: a percentile
    over them counts historical starting months and is not a probability.
    """

    record_first_month: date
    record_last_month: date
    window_starts: tuple[date, ...]


@dataclass(frozen=True)
class Composed:
    """Paths assembled from other models' paths; each component reports its own identity."""

    components: tuple[ModelIdentity, ...]


@dataclass(frozen=True)
class Stipulated:
    """Paths the caller wrote down."""


_PATHS_BY_KIND: Mapping[ModelKind, type[Drawn | Replayed | Composed | Stipulated]] = {
    ModelKind.STRUCTURAL_MACRO: Drawn,
    ModelKind.VECM: Drawn,
    ModelKind.STATE_SPACE: Drawn,
    ModelKind.INDEPENDENT: Drawn,
    ModelKind.TRAINED_PRIVATE_EQUITY: Drawn,
    ModelKind.PRIVATE_EQUITY_RISK: Drawn,
    ModelKind.HISTORICAL_WINDOWS: Replayed,
    ModelKind.COMPOSITE: Composed,
    ModelKind.MIRRORING: Composed,
    ModelKind.STIPULATED: Stipulated,
}


@dataclass(frozen=True)
class ModelIdentity:
    """The model a sampled bundle came from; `name` is the sampler's own label for itself."""

    name: str
    kind: ModelKind
    paths: Drawn | Replayed | Composed | Stipulated

    def __post_init__(self) -> None:
        if not isinstance(self.paths, _PATHS_BY_KIND[self.kind]):
            raise ValueError(f"a {self.kind} model cannot report {type(self.paths).__name__} paths")
