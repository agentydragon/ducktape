"""Batch-only monthly Python policy functions and explicit author-owned memory.

No required base class, registration or closed policy schema. Initializers are
ordinary functions. One call receives an actor's active path observations; each
row gets one ordered action list. Scalar authoring is only an optional Python
adapter, never a second engine interface. Array layout remains a separate choice.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from proposed_augur.accounting import Actor
from proposed_augur.actions import Action
from proposed_augur.observations import Observation

@dataclass(frozen=True)
class Response:
    actions: tuple[Action, ...]

@dataclass(frozen=True)
class PolicyKey:
    """Stable routing identity, supplied by the runner, not batch position."""

    path_id: str
    actor: Actor

@dataclass(frozen=True)
class DecisionKey:
    policy: PolicyKey
    month_index: int

type Policy[S] = Callable[[Mapping[DecisionKey, Observation], S], tuple[Mapping[DecisionKey, Response], S]]
type Initialize = Callable[[tuple[PolicyKey, ...]], tuple[Policy[Any], Any]]

type ScalarPolicy[S] = Callable[[Observation, S], tuple[Response, S]]
type ScalarInitialize = Callable[[PolicyKey], tuple[ScalarPolicy[Any], Any]]

def scalar_to_batch(initialize: ScalarInitialize) -> Initialize:
    """Optional Python adapter returning the same batch-only policy contract.

    Creates fresh scalar function/memory pairs keyed by original actor/path ID.
    Each batch call invokes the scalar function once per supplied observation,
    retaining that row's memory. It makes no executor calls and executes no actions.
    Run and selected replay invoke the returned initializer afresh.
    """
