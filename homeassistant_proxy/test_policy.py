"""Tests for the policy evaluation engine."""

import pytest
import pytest_bazel

from homeassistant_proxy.config import AccessRule, Action, Policy
from homeassistant_proxy.policy import (
    AccessDeniedError,
    EntityInfo,
    EntityRegistry,
    PolicyEnforcer,
    check_entity_access,
)


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


class TestEntityRegistry:
    def test_reverse_device_index(self):
        registry = EntityRegistry(
            {
                "light.a": _info("light.a", device_id="dev_1"),
                "light.b": _info("light.b", device_id="dev_1"),
                "light.c": _info("light.c", device_id="dev_2"),
            }
        )
        assert sorted(registry.entities_for_devices(["dev_1"])) == ["light.a", "light.b"]
        assert registry.entities_for_devices(["dev_2"]) == ["light.c"]
        assert registry.entities_for_devices(["nonexistent"]) == []

    def test_reverse_area_index(self):
        registry = EntityRegistry(
            {
                "light.a": _info("light.a", area_id="kitchen"),
                "light.b": _info("light.b", area_id="kitchen"),
                "light.c": _info("light.c", area_id="bedroom"),
            }
        )
        assert sorted(registry.entities_for_areas(["kitchen"])) == ["light.a", "light.b"]
        assert registry.entities_for_areas(["bedroom"]) == ["light.c"]

    def test_get_unknown_entity_returns_fallback(self):
        registry = EntityRegistry({})
        info = registry.get("sensor.temp")
        assert info.entity_id == "sensor.temp"
        assert info.device_id is None


class TestPolicyEnforcer:
    def test_readable_entities(self):
        policy = Policy(entity_ids={"light.allowed": AccessRule(read=True), "light.denied": AccessRule(read=False)})
        registry = EntityRegistry(
            {
                "light.allowed": _info("light.allowed"),
                "light.denied": _info("light.denied"),
                "light.unknown": _info("light.unknown"),
            }
        )
        enforcer = PolicyEnforcer(policy, registry)
        assert enforcer.readable_entities(["light.allowed", "light.denied", "light.unknown"]) == {"light.allowed"}

    def test_readable_entities_unknown_uses_fallback(self):
        policy = Policy(all=AccessRule(read=True))
        enforcer = PolicyEnforcer(policy, EntityRegistry({}))
        assert enforcer.readable_entities(["sensor.temp"]) == {"sensor.temp"}

    def test_require_read_allowed(self):
        policy = Policy(all=AccessRule(read=True))
        enforcer = PolicyEnforcer(policy, EntityRegistry({}))
        enforcer.require_read("sensor.temp")  # should not raise

    def test_require_read_denied(self):
        policy = Policy()
        enforcer = PolicyEnforcer(policy, EntityRegistry({}))
        with pytest.raises(AccessDeniedError) as exc_info:
            enforcer.require_read("sensor.temp")
        assert "sensor.temp" in exc_info.value.entity_ids

    def test_require_control_allowed(self):
        policy = Policy(all=AccessRule(control=True))
        enforcer = PolicyEnforcer(policy, EntityRegistry({}))
        enforcer.require_control(["light.a", "light.b"])  # should not raise

    def test_require_control_denied(self):
        policy = Policy(entity_ids={"light.ok": AccessRule(control=True), "light.no": AccessRule(control=False)})
        registry = EntityRegistry({"light.ok": _info("light.ok"), "light.no": _info("light.no")})
        enforcer = PolicyEnforcer(policy, registry)
        with pytest.raises(AccessDeniedError) as exc_info:
            enforcer.require_control(["light.ok", "light.no"])
        assert exc_info.value.entity_ids == ["light.no"]

    def test_resolve_targets(self):
        registry = EntityRegistry(
            {
                "light.a": _info("light.a", device_id="dev_1", area_id="kitchen"),
                "light.b": _info("light.b", device_id="dev_1"),
                "switch.c": _info("switch.c", area_id="kitchen"),
            }
        )
        enforcer = PolicyEnforcer(Policy(), registry)
        targets = enforcer.resolve_targets(entity_ids=["sensor.direct"], device_ids=["dev_1"], area_ids=["kitchen"])
        assert "sensor.direct" in targets
        assert "light.a" in targets  # via device
        assert "light.b" in targets  # via device
        assert "switch.c" in targets  # via area


if __name__ == "__main__":
    pytest_bazel.main()
