"""The Home Assistant entity-control boundary: one lamp, and nothing that can reach past it."""

from __future__ import annotations

import pytest
import pytest_bazel

from haku.console.auto_approval.decision import AutoApproved, NotAutoApproved
from haku.console.auto_approval.home_assistant import CALL_SERVICE_TOOL, evaluate_entity_control

LAMP = "light.h6006_pegboard"
ENTITIES = {LAMP: {"turn_on", "turn_off", "toggle"}}


def approve(**arguments):
    return evaluate_entity_control(CALL_SERVICE_TOOL, arguments, ENTITIES)


def test_the_configured_entity_and_service_auto_approve():
    decision = approve(domain="light", service="turn_on", entity_id=LAMP)
    assert isinstance(decision, AutoApproved)
    assert LAMP in decision.explanation


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"brightness": 120}, id="brightness"),
        # The lamp is a Govee H6006: supported_color_modes ["color_temp", "rgb"], plus an effect list.
        pytest.param({"rgb_color": [255, 0, 0]}, id="rgb"),
        pytest.param({"color_temp_kelvin": 2700}, id="colour-temperature"),
        # Effects are the only form that interrupts rather than informs, so they must not be
        # incidentally excluded — see haku-state memory/procedures/transitions.md.
        pytest.param({"effect": "breathe"}, id="effect"),
        pytest.param({"rgb_color": [0, 0, 255], "brightness": 40, "transition": 2}, id="several-at-once"),
    ],
)
def test_service_data_shaping_the_light_is_still_the_same_call(data):
    """Colour, brightness and effects are the point of controlling a lamp; none can retarget it."""
    assert isinstance(approve(domain="light", service="turn_on", entity_id=LAMP, data=data), AutoApproved)


@pytest.mark.parametrize(
    "arguments",
    [
        # The whole reason this policy kind exists: one generic tool reaches every entity.
        pytest.param({"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"}, id="another-entity"),
        pytest.param({"domain": "homeassistant", "service": "restart"}, id="no-target-at-all"),
        pytest.param({"domain": "light", "service": "turn_off"}, id="domain-wide-untargeted"),
        pytest.param({"domain": "light", "service": "turn_on", "entity_id": [LAMP]}, id="list-target"),
        pytest.param(
            {"domain": "light", "service": "turn_on", "entity_id": [LAMP, "light.other"]}, id="list-smuggling-a-second"
        ),
        # A service the lamp is not allowed to receive.
        pytest.param({"domain": "light", "service": "delete", "entity_id": LAMP}, id="unlisted-service"),
        # `homeassistant.turn_off` accepts a light, so the domain must match the entity's own.
        pytest.param({"domain": "homeassistant", "service": "turn_off", "entity_id": LAMP}, id="mismatched-domain"),
    ],
)
def test_calls_that_could_reach_past_the_lamp_stay_manual(arguments):
    assert isinstance(evaluate_entity_control(CALL_SERVICE_TOOL, arguments, ENTITIES), NotAutoApproved)


@pytest.mark.parametrize("key", ["entity_id", "target", "area_id", "device_id", "label_id"])
def test_data_cannot_smuggle_a_second_target(key):
    """Home Assistant resolves any of these to a target set, widening an otherwise-scoped call."""
    decision = approve(domain="light", service="turn_on", entity_id=LAMP, data={key: "light.everything_else"})
    assert isinstance(decision, NotAutoApproved)
    assert key in decision.reason


def test_the_raw_websocket_escape_hatch_stays_manual():
    """`ws_command` bypasses the domain/service/entity triple entirely, so it is never reviewed."""
    decision = approve(domain="light", service="turn_on", entity_id=LAMP, ws_command={"type": "config/entity/remove"})
    assert isinstance(decision, NotAutoApproved)
    assert "ws_command" in decision.reason


def test_an_argument_this_policy_has_never_seen_stays_manual():
    """Allow-list, not deny-list: a field added to the tool later must not silently pass through."""
    decision = approve(domain="light", service="turn_on", entity_id=LAMP, some_future_targeting_field="everything")
    assert isinstance(decision, NotAutoApproved)
    assert "some_future_targeting_field" in decision.reason


def test_only_the_reviewed_tool_is_handled():
    assert isinstance(evaluate_entity_control("ha_bulk_control", {"entity_id": LAMP}, ENTITIES), NotAutoApproved)


if __name__ == "__main__":
    pytest_bazel.main()
