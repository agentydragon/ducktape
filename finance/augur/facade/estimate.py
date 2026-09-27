"""Estimates that carry their own sampling error.

A Monte Carlo figure means nothing without its noise, and a count over overlapping historical
windows is not a probability at all (finance/augur/AGENTS.md § Provenance of a reported number).
Each type holds what was sampled; `str()` is its one rendering, which rounds the error to one
significant figure and never prints the estimate past that figure's decimal place.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from statistics import NormalDist

import numpy as np
from scipy.stats import binomtest
from statsmodels.stats.proportion import proportion_confint

# Doubles carry about 16 significant digits and path arithmetic loses a few, so an error at or below
# this fraction of an estimate's scale (taken as at least 1) is rounding, not sampling: the estimate
# is exact. Any other error is above 1e-12, so no rendering prints past the 12th decimal.
_FLOAT_NOISE = 1e-12


@dataclass(frozen=True)
class Proportion:
    """`successes` of `paths` independent paths, with a Wilson score interval at `confidence`."""

    successes: int
    paths: int
    confidence: float

    def __post_init__(self) -> None:
        if not 0 <= self.successes <= self.paths or self.paths < 1:
            raise ValueError(f"need 0 <= successes <= paths and at least one path: {self.successes=} {self.paths=}")
        if not 0 < self.confidence < 1:
            raise ValueError(f"confidence must lie in (0, 1): {self.confidence=}")

    @property
    def fraction(self) -> float:
        return self.successes / self.paths

    @property
    def interval(self) -> tuple[float, float]:
        low, high = proportion_confint(self.successes, self.paths, alpha=1 - self.confidence, method="wilson")
        return float(low), float(high)

    def __str__(self) -> str:
        low, high = self.interval
        # The interval is this estimate's error bar, so its half-width sets the last printed digit.
        decimals = _decimals((high - low) / 2 * 100)
        return (
            f"{_fixed(self.fraction * 100, decimals)}% ({self.confidence * 100:g}% CI "
            f"{_fixed(low * 100, decimals)}-{_fixed(high * 100, decimals)}%, {self.successes} of {self.paths} paths)"
        )


@dataclass(frozen=True)
class Mean:
    """Sample mean over `paths` independent paths, with its standard error."""

    mean: float
    standard_error: float
    paths: int

    def __post_init__(self) -> None:
        _require_standard_error(self.mean, self.standard_error, self.paths)

    @classmethod
    def of(cls, values: Iterable[float]) -> Mean:
        """One value per independent path; the error is the sample standard deviation over √n."""
        array = np.fromiter(values, dtype=np.float64)
        if array.size < 2:
            raise ValueError(f"a standard error needs at least two paths: {array.size=}")
        return cls(
            mean=float(array.mean()), standard_error=float(array.std(ddof=1)) / math.sqrt(array.size), paths=array.size
        )

    def __str__(self) -> str:
        mean, error = _plus_minus(self.mean, self.standard_error, signed=False)
        return f"{mean} ± {error} (SE, {self.paths} paths)"


@dataclass(frozen=True)
class ProportionDifference:
    """`a - b` between two success proportions measured on the same `paths` paths.

    Only discordant paths inform the difference: `a_only` paths where `a` succeeded and `b` did not,
    `b_only` the reverse. The standard error is the Wald one from those counts (Agresti, Categorical
    Data Analysis, 2nd ed., §10.1) and is for display only: it understates the noise when discordant
    paths are few and is zero when every path is discordant the same way, so `verdict` decides by an
    exact test instead.
    """

    paths: int
    a_only: int
    b_only: int

    def __post_init__(self) -> None:
        if min(self.a_only, self.b_only) < 0 or self.a_only + self.b_only > self.paths or self.paths < 1:
            raise ValueError(f"discordant counts must fit in the paths: {self.a_only=} {self.b_only=} {self.paths=}")

    @classmethod
    def from_paths(cls, *, a: Mapping[int, bool], b: Mapping[int, bool]) -> ProportionDifference:
        """Each arm maps path ID to whether it succeeded there; paths pair by ID."""
        _require_same_paths(a, b)
        return cls(
            paths=len(a),
            a_only=sum(a[path] and not b[path] for path in a),
            b_only=sum(b[path] and not a[path] for path in a),
        )

    @property
    def difference(self) -> float:
        return (self.a_only - self.b_only) / self.paths

    @property
    def standard_error(self) -> float:
        net = self.a_only - self.b_only
        return math.sqrt(self.a_only + self.b_only - net * net / self.paths) / self.paths

    def __str__(self) -> str:
        difference, error = _plus_minus(self.difference * 100, self.standard_error * 100, signed=True)
        return f"{difference}pp ± {error}pp (SE, {self.paths} paired paths)"


@dataclass(frozen=True)
class MeanDifference:
    """`a - b` between two means measured on the same `paths` paths.

    It is the mean of the per-path differences, so their spread, not the arms' own, sets the error.
    """

    difference: float
    standard_error: float
    paths: int

    def __post_init__(self) -> None:
        _require_standard_error(self.difference, self.standard_error, self.paths)

    @classmethod
    def from_paths(cls, *, a: Mapping[int, float], b: Mapping[int, float]) -> MeanDifference:
        """Each arm maps path ID to its value there; paths pair by ID."""
        _require_same_paths(a, b)
        per_path = Mean.of(a[path] - b[path] for path in a)
        return cls(difference=per_path.mean, standard_error=per_path.standard_error, paths=per_path.paths)

    def __str__(self) -> str:
        difference, error = _plus_minus(self.difference, self.standard_error, signed=True)
        return f"{difference} ± {error} (SE, {self.paths} paired paths)"


type PairedDifference = ProportionDifference | MeanDifference


@dataclass(frozen=True)
class WindowFrequency:
    """`successes` of `windows` overlapping historical windows, worth about `independent_windows` independent ones.

    Consecutive windows share nearly all their months, so the counted windows are mostly a few
    episodes, each counted many times over. There is deliberately no fraction, probability or
    interval: a replay count is quoted as a count, beside the effective sample it came from.
    """

    successes: int
    windows: int
    independent_windows: float

    def __post_init__(self) -> None:
        if not 0 <= self.successes <= self.windows:
            raise ValueError(f"need 0 <= successes <= windows: {self.successes=} {self.windows=}")
        if not 0 < self.independent_windows <= self.windows:
            raise ValueError(f"need 0 < independent_windows <= windows: {self.independent_windows=} {self.windows=}")

    def __str__(self) -> str:
        return f"{self.successes} of {self.windows} overlapping windows (≈{self.independent_windows:.1f} independent)"


type Estimate = Proportion | Mean | PairedDifference | WindowFrequency


class Direction(StrEnum):
    A_HIGHER = "a_higher"
    B_HIGHER = "b_higher"


@dataclass(frozen=True)
class Resolved:
    direction: Direction


@dataclass(frozen=True)
class Unresolved:
    """`a - b` lies within the stated number of standard errors of zero: the sample does not order the arms."""


type Verdict = Resolved | Unresolved


def verdict(paired: PairedDifference, *, standard_errors: float) -> Verdict:
    """Resolved when `a - b` differs from zero at the significance of a `standard_errors`-SE normal test.

    A mean difference must exceed `standard_errors` standard errors, and float noise. A proportion
    difference is decided by the exact conditional McNemar test, a two-sided binomial test of `a_only`
    against `b_only` at 1/2, which must reach the same level `2 * (1 - Phi(standard_errors))`.
    """
    if not 0 < standard_errors < math.inf:
        raise ValueError(f"standard_errors must be positive and finite: {standard_errors=}")
    if isinstance(paired, ProportionDifference):
        discordant = paired.a_only + paired.b_only
        resolved = discordant > 0 and (
            binomtest(paired.a_only, discordant, p=0.5).pvalue < 2 * NormalDist().cdf(-standard_errors)
        )
    else:
        resolved = abs(paired.difference) > max(standard_errors * paired.standard_error, _FLOAT_NOISE)
    if not resolved:
        return Unresolved()
    return Resolved(Direction.A_HIGHER if paired.difference > 0 else Direction.B_HIGHER)


def _require_same_paths(a: Mapping[int, object], b: Mapping[int, object]) -> None:
    if a.keys() != b.keys():
        raise ValueError(f"arms were evaluated on different paths; in one arm only: {sorted(a.keys() ^ b.keys())[:10]}")


def _require_standard_error(estimate: float, standard_error: float, paths: int) -> None:
    if paths < 2 or not math.isfinite(estimate) or not math.isfinite(standard_error) or standard_error < 0:
        raise ValueError(
            "need a finite estimate, a finite non-negative standard error and at least two paths: "
            f"{estimate=} {standard_error=} {paths=}"
        )


def _decimals(error: float) -> int:
    """Decimal place of `error`'s first significant figure; negative for tens, hundreds, ...

    `.0e` rounds to one significant figure and carries into the exponent: 0.096 formats as `1e-01`.
    """
    return -int(f"{error:.0e}".partition("e")[2])


def _fixed(value: float, decimals: int, *, signed: bool = False) -> str:
    # `z` prints a value that rounds to zero as 0, never -0.
    return f"{round(value, decimals):{'+' if signed else ''}z,.{max(decimals, 0)}f}"


def _plus_minus(value: float, error: float, *, signed: bool) -> tuple[str, str]:
    """`value` and `error`, both to the decimal place of the error's first significant figure.

    An error within float noise leaves `value` exact: it prints to the noise floor without trailing
    zeros, so a difference that is constant up to rounding reads as a clean number.
    """
    noise = _FLOAT_NOISE * max(1.0, abs(value))
    if error <= noise:
        exact = _fixed(value, _decimals(noise), signed=signed)
        return exact.rstrip("0").rstrip(".") if "." in exact else exact, "0"
    decimals = _decimals(error)
    return _fixed(value, decimals, signed=signed), _fixed(error, decimals)
