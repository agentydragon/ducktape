"""Two experiment-owned authoring styles for the same annual spending rule.

Both use exact integer rounding, not float portfolio arithmetic. The batch form uses
NumPy object arrays (Python integers), not a claim of SIMD or compiled-policy speed.
Neither implementation performs financial settlement or computes tax.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from finance.augur.rust.simulator import PrototypeSpendingSession, SpendingObservationBatch


@dataclass(frozen=True)
class Parameters:
    rate_bps: int
    max_cut_bps: int
    max_raise_bps: int

    def __post_init__(self) -> None:
        if (
            not 0 < self.rate_bps <= 10_000
            or not 0 <= self.max_cut_bps <= 10_000
            or not 0 <= self.max_raise_bps <= 10_000
        ):
            raise ValueError("rate must be in (0, 10000] bps; cut and raise in [0, 10000] bps")


@dataclass(frozen=True)
class Observation:
    month: int
    cash: int
    public_holdings: int
    cpi: int


@dataclass(frozen=True)
class Observations:
    """Experiment's batch projection; original IDs route memory, not economic decisions."""

    rollout_ids: NDArray[np.int64]
    months: NDArray[np.int64]
    cash: NDArray[np.object_]
    public_holdings: NDArray[np.object_]
    cpi: NDArray[np.object_]

    @classmethod
    def from_native(cls, batch: SpendingObservationBatch) -> "Observations":
        return cls(
            np.asarray(batch.rollout_ids, dtype=np.int64),
            np.asarray(batch.months, dtype=np.int64),
            np.asarray(batch.cash, dtype=object),
            np.asarray(batch.public_holdings, dtype=object),
            np.asarray(batch.price_numerators, dtype=object),
        )

    def select(self, rows: slice) -> "Observations":
        return Observations(
            self.rollout_ids[rows], self.months[rows], self.cash[rows], self.public_holdings[rows], self.cpi[rows]
        )


def _checked(value: int) -> int:
    if not -(1 << 63) <= value < 1 << 63:
        raise OverflowError("spending policy amount is outside signed 64-bit currency quanta")
    return value


def _scale(value: int, numerator: int, denominator: int) -> int:
    # Inputs are integer money and positive CPI/denominators. Match native Money's
    # half-away-from-zero rounding and check each intermediate money result.
    rounded = (2 * abs(value) * numerator + denominator) // (2 * denominator)
    return _checked(-rounded if value < 0 else rounded)


class ScalarPolicy:
    """One ordinary stateful decision function, independently instantiated per path."""

    def __init__(self, parameters: Parameters) -> None:
        self.parameters = parameters
        self.previous: tuple[int, int] | None = None

    def __call__(self, observation: Observation) -> int:
        if observation.month % 12:
            return 0
        target = _scale(_checked(observation.cash + observation.public_holdings), self.parameters.rate_bps, 10_000)
        request = target
        if self.previous is not None:
            previous, cpi = self.previous
            indexed = _scale(previous, observation.cpi, cpi)
            lower = _scale(indexed, 10_000 - self.parameters.max_cut_bps, 10_000)
            upper = _scale(indexed, 10_000 + self.parameters.max_raise_bps, 10_000)
            request = min(max(target, lower), upper)
        self.previous = request, observation.cpi
        return request


class ScalarAdapter:
    """Route batch rows to independent scalar functions using original path IDs."""

    def __init__(self, parameters: Parameters, rollout_ids: list[int]) -> None:
        self.policies = {id_: ScalarPolicy(parameters) for id_ in rollout_ids}

    def __call__(self, observations: Observations) -> list[int]:
        return [
            self.policies[int(id_)](Observation(int(month), cash, public, cpi))
            for id_, month, cash, public, cpi in zip(
                observations.rollout_ids,
                observations.months,
                observations.cash,
                observations.public_holdings,
                observations.cpi,
                strict=True,
            )
        ]


def _checked_batch(values: NDArray[np.object_]) -> NDArray[np.object_]:
    if np.any((values < -(1 << 63)) | (values >= 1 << 63)):
        raise OverflowError("spending policy amount is outside signed 64-bit currency quanta")
    return values


def _scale_batch(
    values: NDArray[np.object_], numerator: int | NDArray[np.object_], denominator: int | NDArray[np.object_]
) -> NDArray[np.object_]:
    rounded = (2 * np.abs(values) * numerator + denominator) // (2 * denominator)
    return _checked_batch(np.where(values < 0, -rounded, rounded))


class BatchPolicy:
    """A batch-authored rule with independent memory indexed by original path ID.

    Object arrays deliberately preserve exact wide intermediates. Replacing them with
    int64 without checked arithmetic would silently change large-portfolio decisions.
    """

    def __init__(self, parameters: Parameters, rollout_count: int) -> None:
        self.parameters = parameters
        self.initialized = np.zeros(rollout_count, dtype=np.bool_)
        self.previous = np.zeros(rollout_count, dtype=object)
        self.cpi = np.ones(rollout_count, dtype=object)

    def __call__(self, observations: Observations) -> list[int]:
        requests = np.zeros(len(observations.rollout_ids), dtype=object)
        annual = observations.months % 12 == 0
        ids = observations.rollout_ids[annual]
        if not len(ids):
            return requests.tolist()
        target = _scale_batch(
            _checked_batch(observations.cash[annual] + observations.public_holdings[annual]),
            self.parameters.rate_bps,
            10_000,
        )
        # Reset only initialized paths: uninitialized memory is not an economic amount.
        repeated = self.initialized[ids]
        previous_ids = ids[repeated]
        indexed = _scale_batch(self.previous[previous_ids], observations.cpi[annual][repeated], self.cpi[previous_ids])
        lower = _scale_batch(indexed, 10_000 - self.parameters.max_cut_bps, 10_000)
        upper = _scale_batch(indexed, 10_000 + self.parameters.max_raise_bps, 10_000)
        target[repeated] = np.minimum(np.maximum(target[repeated], lower), upper)
        requests[annual] = target
        self.previous[ids] = target
        self.cpi[ids] = observations.cpi[annual]
        self.initialized[ids] = True
        return requests.tolist()


def run(
    input_json: str,
    policy: Callable[[Observations], list[int]],
    rollout_ids: list[int],
    *,
    forensic: bool = False,
    chunk_size: int | None = None,
    reverse: bool = False,
) -> dict[str, Any]:
    """Own the monthly outer loop; pass a fresh policy instance for each run/replay.

    The callable can be edited/replaced in a notebook without rebuilding Rust. Each
    path is decided once per month; independent chunks have no cross-path feedback.
    Configured native funding/tax mechanics still execute all financial effects.
    """
    if chunk_size is not None and chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    session = PrototypeSpendingSession(
        input_json,
        json.dumps(
            {
                "from": {"agent_id": "retiree", "account_id": "checking"},
                "to": {"agent_id": "world", "account_id": "checking"},
                "cause_id": "annual_consumption",
            }
        ),
        rollout_ids,
        forensic=forensic,
    )
    try:
        while (native := session.observe()).rollout_ids:
            observations = Observations.from_native(native)
            if reverse:
                observations = observations.select(slice(None, None, -1))
            size = chunk_size or len(observations.rollout_ids)
            for start in range(0, len(observations.rollout_ids), size):
                chunk = observations.select(slice(start, start + size))
                amounts = policy(chunk)
                session.advance(list(zip(chunk.rollout_ids.tolist(), chunk.months.tolist(), amounts, strict=True)))
        return cast(dict[str, Any], json.loads(session.finish_json()))
    finally:
        session.close()
