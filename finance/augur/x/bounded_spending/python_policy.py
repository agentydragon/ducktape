"""Two experiment-owned authoring styles for the same annual spending rule.

Both use exact integer rounding, not float portfolio arithmetic. The batch form uses
NumPy object arrays (Python integers), not a claim of SIMD or compiled-policy speed.
Neither implementation performs financial settlement or computes tax.
"""

from collections.abc import Callable
from dataclasses import dataclass
from itertools import batched
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from finance.augur.policy.sleeves import withdraw
from finance.augur.rust.simulator import Action, ActionSession, Decision, DecisionActions
from finance.augur.sim.prepared import CompiledRun
from finance.augur.sim.results import ConsumptionTarget, Finished


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
    def from_native(cls, batch: list[Decision]) -> "Observations":
        cpi = []
        for row in batch:
            level = row.observation.cpi
            if level is None:
                raise ValueError("bounded spending requires a supplied CPI path")
            cpi.append(level[0])
        return cls(
            np.asarray([row.rollout_id for row in batch], dtype=np.int64),
            np.asarray([row.observation.month for row in batch], dtype=np.int64),
            np.asarray([row.observation.cash for row in batch], dtype=object),
            np.asarray([row.observation.public_holdings for row in batch], dtype=object),
            np.asarray(cpi, dtype=object),
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


class SpendingPolicy:
    """Sales-only funding proposals, then due-claim payments, then chosen consumption.

    The rule sees current cashflows and claims. Failed execution retains its successful
    prefix; this policy never asks the engine to retry, cut spending or allocate for it.
    """

    def __init__(self, rule: Callable[[Observations], list[int]], targets: dict[tuple[str, str], int]) -> None:
        self.rule = rule
        self.targets = targets

    def __call__(self, batch: list[Decision]) -> list[DecisionActions]:
        responses = []
        for decision, amount in zip(batch, self.rule(Observations.from_native(batch)), strict=True):
            observation = decision.observation
            claims = observation.claims
            cause = f"annual_consumption_m{observation.month}"
            actions = (
                withdraw(
                    observation,
                    targets=self.targets,
                    cash_account_id="checking",
                    amount=max(
                        0, amount + sum(claim.amount_due for claim in claims) - dict(observation.accounts)["checking"]
                    ),
                    cause_id=f"fund-{cause}",
                )
                if self.targets
                else []
            )
            actions.extend(
                Action.pay_claim(index, f"pay-{claim.cause_id}", claim, claim.from_account, claim.amount_due)
                for index, claim in enumerate(claims)
            )
            if amount:
                actions.append(
                    Action.consume(
                        len(claims),
                        cause,
                        "annual_consumption",
                        (observation.agent_id, "checking"),
                        ("world", "checking"),
                        amount,
                    )
                )
            responses.append(DecisionActions(decision.rollout_id, observation.month, actions))
        return responses


def run(
    prepared: CompiledRun,
    policy: Callable[[list[Decision]], list[DecisionActions]],
    rollout_ids: list[int],
    *,
    capture: Literal["summary", "dense", "forensic"] = "summary",
    chunk_size: int | None = None,
    reverse: bool = False,
) -> Finished:
    """Python owns the loop; chunks author one complete response before each advance."""
    if chunk_size is not None and chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    session = ActionSession(prepared, "retiree", rollout_ids, capture=capture)
    try:
        batch = session.start()
        while not isinstance(batch, Finished):
            if reverse:
                batch.reverse()
            responses = [
                response
                for chunk in batched(batch, chunk_size or len(batch), strict=False)
                for response in policy(list(chunk))
            ]
            batch = session.advance(responses)
        return batch
    finally:
        session.close()


def consumption(output: Finished) -> tuple[list[list[int | None]], list[list[int]]]:
    """Project attempted requests and actual payments over each path's observed months."""
    requested = []
    paid = []
    for rollout in output.rollouts:
        summary = rollout.summary
        amounts: list[int | None] = [0] * summary.ending_book.month
        receipts = [0] * len(amounts)
        # This policy omits live zero requests. On a stopped month, absence can also
        # mean an earlier action prevented consumption; the request is absent,
        # but the complete execution prefix proves actual payment is zero.
        if rollout.stop is not None:
            amounts[-1] = None
        for payment in summary.payments:
            receipt = payment.receipt
            if isinstance(receipt.target, ConsumptionTarget) and receipt.target.component_id == "annual_consumption":
                amounts[payment.month] = receipt.amount_requested
                receipts[payment.month] = receipt.amount_paid
        requested.append(amounts)
        paid.append(receipts)
    return requested, paid
