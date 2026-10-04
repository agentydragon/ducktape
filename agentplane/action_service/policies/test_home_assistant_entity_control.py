"""`home_assistant_entity_control`: one lamp, and nothing that can reach past it."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from pydantic import JsonValue, ValidationError

from agentplane.action_service.policies.home_assistant_entity_control import HomeAssistantEntityControl, evaluate
from agentplane.action_service.policies.kind import Matched, NotMatched

LAMP = "light.test_lamp"
SERVICES = ["turn_on", "turn_off", "toggle"]
ACTIONS = {"ha-test": ["ha_call_service"]}
POLICY = HomeAssistantEntityControl.model_validate(
    {"type": "home_assistant_entity_control", "actions": ACTIONS, "entities": {LAMP: SERVICES}}
)


def decide(**arguments: JsonValue) -> Matched | NotMatched:
    return evaluate(POLICY, arguments)


def test_the_configured_entity_and_service_match() -> None:
    decision = decide(domain="light", service="turn_on", entity_id=LAMP)
    assert isinstance(decision, Matched)
    assert LAMP in decision.explanation


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({}, id="no-data"),
        pytest.param({"data": None}, id="null-data"),
        pytest.param({"data": {"rgb_color": [255, 0, 0], "brightness": 40, "transition": 2}}, id="shaping-data"),
        pytest.param({"wait": True, "verbose": False}, id="reviewed-options"),
    ],
)
def test_arguments_that_only_shape_the_call_still_match(extra: dict[str, JsonValue]) -> None:
    assert isinstance(decide(domain="light", service="turn_on", entity_id=LAMP, **extra), Matched)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"domain": "lock", "service": "unlock", "entity_id": "lock.test_door"}, id="another-entity"),
        pytest.param({"domain": "homeassistant", "service": "restart"}, id="no-target-at-all"),
        pytest.param({"domain": "light", "service": "turn_off"}, id="domain-wide-untargeted"),
        pytest.param({"domain": "light", "service": "turn_on", "entity_id": ""}, id="empty-target"),
        pytest.param({"domain": "light", "service": "turn_on", "entity_id": [LAMP]}, id="list-target"),
        pytest.param(
            {"domain": "light", "service": "turn_on", "entity_id": [LAMP, "light.test_other"]}, id="list-with-a-second"
        ),
        pytest.param({"domain": "light", "service": "delete", "entity_id": LAMP}, id="unlisted-service"),
        pytest.param({"domain": "light", "entity_id": LAMP}, id="no-service"),
        pytest.param({"domain": "light", "service": ["turn_on"], "entity_id": LAMP}, id="list-service"),
        # `homeassistant.turn_off` accepts a light, so the domain must be the entity's own.
        pytest.param({"domain": "homeassistant", "service": "turn_off", "entity_id": LAMP}, id="mismatched-domain"),
        pytest.param({"service": "turn_on", "entity_id": LAMP}, id="no-domain"),
    ],
)
def test_calls_that_could_reach_past_the_lamp_do_not_match(arguments: dict[str, JsonValue]) -> None:
    assert isinstance(evaluate(POLICY, arguments), NotMatched)


@pytest.mark.parametrize("key", ["entity_id", "target", "area_id", "device_id", "label_id"])
def test_data_cannot_retarget_the_call(key: str) -> None:
    decision = decide(domain="light", service="turn_on", entity_id=LAMP, data={key: "light.test_everything_else"})
    assert isinstance(decision, NotMatched)
    assert key in decision.reason


@pytest.mark.parametrize(
    "data",
    [
        pytest.param('{"entity_id": "light.test_other"}', id="json-string"),
        pytest.param([], id="empty-list"),
        pytest.param("", id="empty-string"),
    ],
)
def test_data_that_is_not_an_object_does_not_match(data: JsonValue) -> None:
    assert isinstance(decide(domain="light", service="turn_on", entity_id=LAMP, data=data), NotMatched)


@pytest.mark.parametrize("argument", ["ws_command", "some_future_targeting_field"])
def test_an_argument_this_policy_has_not_reviewed_does_not_match(argument: str) -> None:
    """An allow-list: `ws_command` is a raw websocket escape hatch, and a field the tool grows
    later must not pass through unseen."""
    arguments: dict[str, JsonValue] = {
        "domain": "light",
        "service": "turn_on",
        "entity_id": LAMP,
        argument: {"type": "config/anything"},
    }
    decision = evaluate(POLICY, arguments)
    assert isinstance(decision, NotMatched)
    assert argument in decision.reason


@pytest.mark.parametrize("names", [["ha_bulk_control"], ["ha_call_service", "ha_bulk_control"]])
def test_only_the_service_call_tool_can_be_listed(names: list[str]) -> None:
    with pytest.raises(ValidationError, match="only ha_call_service is reviewed"):
        HomeAssistantEntityControl.model_validate(
            {"type": "home_assistant_entity_control", "actions": {"ha-test": names}, "entities": {LAMP: SERVICES}}
        )


@pytest.mark.parametrize(
    "entities",
    [
        pytest.param({}, id="none"),
        pytest.param({"light": ["turn_on"]}, id="not-domain-qualified"),
        pytest.param({"light.test_a,light.test_b": ["turn_on"]}, id="several-ids-in-one-string"),
        pytest.param({LAMP: []}, id="no-services"),
        pytest.param({LAMP: ["light.turn_on"]}, id="domain-qualified-service"),
    ],
)
def test_entities_must_name_one_entity_and_plain_services(entities: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="entities"):
        HomeAssistantEntityControl.model_validate(
            {"type": "home_assistant_entity_control", "actions": ACTIONS, "entities": entities}
        )


if __name__ == "__main__":
    pytest_bazel.main()
