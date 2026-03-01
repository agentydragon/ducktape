"""Tests for the policy evaluation engine."""

import pytest_bazel

from homeassistant_proxy.config import AccessRule, Action, Policy
from homeassistant_proxy.policy import EntityInfo, check_all_entities, check_entity_access, filter_entities


def _info(entity_id: str, device_id: str | None = None, area_id: str | None = None) -> EntityInfo:
    return EntityInfo(entity_id=entity_id, device_id=device_id, area_id=area_id)


class TestCheckEntityAccess:
    def test_default_deny(self):
        policy = Policy()
        info = _info("light.kitchen")
        assert not check_entity_access("light.kitchen", Action.READ, policy, info)
        assert not check_entity_access("light.kitchen", Action.CONTROL, policy, info)

    def test_all_allows_read(self):
        policy = Policy(all=AccessRule(read=True))
        info = _info("light.kitchen")
        assert check_entity_access("light.kitchen", Action.READ, policy, info)
        assert not check_entity_access("light.kitchen", Action.CONTROL, policy, info)

    def test_domain_overrides_all(self):
        policy = Policy(
            all=AccessRule(read=True, control=False), domains={"light": AccessRule(read=True, control=True)}
        )
        info = _info("light.kitchen")
        assert check_entity_access("light.kitchen", Action.CONTROL, policy, info)
        # switch domain falls back to all
        switch_info = _info("switch.pump")
        assert not check_entity_access("switch.pump", Action.CONTROL, policy, switch_info)

    def test_area_overrides_domain(self):
        policy = Policy(
            domains={"light": AccessRule(read=True, control=False)},
            area_ids={"bedroom": AccessRule(read=True, control=True)},
        )
        info = _info("light.bedroom_lamp", area_id="bedroom")
        assert check_entity_access("light.bedroom_lamp", Action.CONTROL, policy, info)

    def test_device_overrides_area(self):
        policy = Policy(
            area_ids={"bedroom": AccessRule(read=True, control=True)},
            device_ids={"dev_123": AccessRule(read=True, control=False)},
        )
        info = _info("light.bedroom_lamp", device_id="dev_123", area_id="bedroom")
        assert not check_entity_access("light.bedroom_lamp", Action.CONTROL, policy, info)

    def test_entity_overrides_device(self):
        policy = Policy(
            device_ids={"dev_123": AccessRule(read=True, control=False)},
            entity_ids={"light.special": AccessRule(read=True, control=True)},
        )
        info = _info("light.special", device_id="dev_123")
        assert check_entity_access("light.special", Action.CONTROL, policy, info)

    def test_entity_override_denies_even_when_device_allows(self):
        policy = Policy(
            device_ids={"dev_123": AccessRule(read=True, control=True)},
            entity_ids={"light.dangerous": AccessRule(read=True, control=False)},
        )
        info = _info("light.dangerous", device_id="dev_123")
        assert not check_entity_access("light.dangerous", Action.CONTROL, policy, info)


class TestFilterEntities:
    def test_filters_by_read(self):
        policy = Policy(entity_ids={"light.allowed": AccessRule(read=True), "light.denied": AccessRule(read=False)})
        registry = {
            "light.allowed": _info("light.allowed"),
            "light.denied": _info("light.denied"),
            "light.unknown": _info("light.unknown"),
        }
        result = filter_entities(["light.allowed", "light.denied", "light.unknown"], Action.READ, policy, registry)
        assert result == ["light.allowed"]

    def test_unknown_entity_uses_fallback(self):
        policy = Policy(all=AccessRule(read=True))
        result = filter_entities(["sensor.temp"], Action.READ, policy, {})
        assert result == ["sensor.temp"]


class TestCheckAllEntities:
    def test_returns_denied_entities(self):
        policy = Policy(entity_ids={"light.ok": AccessRule(control=True), "light.no": AccessRule(control=False)})
        registry = {"light.ok": _info("light.ok"), "light.no": _info("light.no")}
        denied = check_all_entities(["light.ok", "light.no"], Action.CONTROL, policy, registry)
        assert denied == ["light.no"]

    def test_empty_when_all_allowed(self):
        policy = Policy(all=AccessRule(control=True))
        denied = check_all_entities(["light.a", "light.b"], Action.CONTROL, policy, {})
        assert denied == []


if __name__ == "__main__":
    pytest_bazel.main()
