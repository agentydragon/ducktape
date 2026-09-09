"""Ordinary monthly Python functions and explicit actor/path-local memory.

No required base class, registration or closed policy schema. Initializers are
ordinary functions; optional portfolio calculators live separately. The scalar
signature specifies meaning, not the representation of high-N batch transport.
"""

from collections.abc import Callable
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

type Policy[S] = Callable[[Observation, S], tuple[Response, S]]
type Initialize = Callable[[PolicyKey], tuple[Policy[Any], Any]]
