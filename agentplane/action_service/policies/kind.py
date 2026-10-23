"""The common Pydantic base for operator-authored Action policy shapes.

The Go runtime evaluates the typed variants; Python validates and renders the wire models.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentplane.action_service.catalog import Key


class Spec(BaseModel):
    """Operator-authored fields: an unknown key is a mistake, never ignored."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class Kind(Spec):
    """One kind's parameters; `type` names the variant and `actions` lists its Actions."""

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
