"""Model validation for `home_assistant_entity_control` policy shapes."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_bazel
from pydantic import ValidationError

from agentplane.action_service.policies.home_assistant_entity_control import HomeAssistantEntityControl

LAMP = "light.test_lamp"
SERVICES = ["turn_on", "turn_off", "toggle"]
ACTIONS = {"ha-test": ["ha_call_service"]}


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
