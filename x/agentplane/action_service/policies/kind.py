"""What every policy kind shares: the operator-authored model its wire shape extends, and the
answer its evaluator gives.

A kind is one typed evaluator: YAML carries `type` and parameters, Python owns the semantics.
Adding a constraint the YAML cannot express means adding a kind, never a DSL.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

from x.agentplane.action_service.catalog import Key


class Spec(BaseModel):
    """Operator-authored fields: an unknown key is a mistake, never ignored."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class Kind(Spec):
    """One kind's parameters; `type` names the evaluator, `actions` bounds it to listed Actions."""

    actions: dict[Key, frozenset[Key]] = Field(
        min_length=1, description="Action names by ActionGroup key; the policy matches only these."
    )

    @field_validator("actions")
    @classmethod
    def _named_actions(cls, value: dict[Key, frozenset[Key]]) -> dict[Key, frozenset[Key]]:
        for group, names in value.items():
            if not names:
                raise ValueError(f"group {group!r} lists no actions")
        return value


@dataclass(frozen=True, slots=True)
class Matched:
    explanation: str


@dataclass(frozen=True, slots=True)
class NotMatched:
    reason: str
