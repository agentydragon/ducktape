"""The PolicySets a Sandbox names, with every rule they hold, whichever reader enforces it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic.alias_generators import to_camel

from agentplane.policy_sets import ObjectMeta


class _RawRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str


class _RawSpec(BaseModel):
    rules: list[dict[str, JsonValue]]


class _RawPolicySet(BaseModel):
    model_config = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)

    metadata: ObjectMeta
    spec: _RawSpec


class PolicyRuleView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(description="The rule's position among all of the set's rules, of every type.")
    type: str = Field(description="The rule type, which selects the reader that enforces it.")
    fields: dict[str, JsonValue] = Field(description="The rule's other fields, as the PolicySet states them.")


class BoundPolicySetView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    generation: int
    rules: list[PolicyRuleView]


class BoundPolicyView(BaseModel):
    """What a Sandbox's PolicySets grant: all of their rules, unlike the Action Service's answer,
    which holds only the rules it enforces."""

    model_config = ConfigDict(extra="forbid")

    policy_sets: list[BoundPolicySetView] = Field(description="The named sets that exist, in name order.")
    missing_policy_sets: list[str] = Field(description="Names the Sandbox lists that no PolicySet answers to.")


def bound_policy_view(names: Set[str], policy_sets: Iterable[object]) -> BoundPolicyView:
    parsed: Mapping[str, _RawPolicySet] = {
        policy_set.metadata.name: policy_set for policy_set in map(_RawPolicySet.model_validate, policy_sets)
    }
    return BoundPolicyView(
        policy_sets=[
            BoundPolicySetView(
                name=name,
                generation=parsed[name].metadata.generation,
                rules=[
                    PolicyRuleView(
                        number=number,
                        type=rule.type,
                        fields={key: value for key, value in raw.items() if key != "type"},
                    )
                    for number, raw in enumerate(parsed[name].spec.rules)
                    if (rule := _RawRule.model_validate(raw))
                ],
            )
            for name in sorted(names & parsed.keys())
        ],
        missing_policy_sets=sorted(names - parsed.keys()),
    )
