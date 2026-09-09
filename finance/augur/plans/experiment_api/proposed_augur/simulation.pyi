"""Canonical financial stepping, with an experiment-owned monthly loop.

One observation/call per active actor/month. Advance executes action lists in
caller order, preserving product cash availability. An unexecutable action is
atomic and fatal only for its rollout: retain successful prefix, skip later actions
and never call that path's policies again. No retry, automatic funding, cuts,
borrowing or hidden allocation. Actor ordering is an environment rule, not row order.

The batched boundary does not choose array layout or require a native executor.
RUNTIME/GE considers language separately from policy-call and ragged-output layout.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from proposed_augur.accounting import Actor
from proposed_augur.markets import Worlds
from proposed_augur.observations import Observation
from proposed_augur.policies import Initialize, PolicyKey, Response
from proposed_augur.results import Observer, PathResults, Run
from proposed_augur.state import Situation

@dataclass(frozen=True)
class DecisionKey:
    policy: PolicyKey
    month_index: int

@dataclass(frozen=True)
class DecisionBatch:
    observations: Mapping[DecisionKey, Observation]

@dataclass(frozen=True)
class Finished:
    result: PathResults

class Session:
    def __init__(
        self,
        situation: Situation,
        *,
        worlds: Worlds,
        reporting_actor: Actor,
        decision_actors: tuple[Actor, ...],
        observers: Mapping[str, Observer],
    ) -> None: ...
    def start(self) -> DecisionBatch | Finished: ...
    def advance(self, responses: Mapping[DecisionKey, Response]) -> DecisionBatch | Finished:
        """Require exactly this pending month's keys; reject stale/duplicate/missing responses."""

def run(
    situation: Situation,
    *,
    policies: Mapping[Actor, Initialize],
    reporting_actor: Actor,
    worlds: Worlds,
    observers: Mapping[str, Observer],
) -> Run:
    """Optional monthly loop using precisely the Session contract.

    Initializes fresh memory per original actor/path identity, including trace
    replay; never serializes closures or mutates reused worlds/situations.
    """
