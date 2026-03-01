"""Tests for the policy evaluation engine."""

import pytest
import pytest_bazel

from homeassistant_proxy.config import AccessRule, Action, Policy
from homeassistant_proxy.policy import AccessDeniedError, EntityInfo, EntityRegistry, PolicyEnforcer


def _info(entity_id: str, device_id: str | None = None, area_id: str | None = None) -> EntityInfo:
    return EntityInfo(entity_id=entity_id, device_id=device_id, area_id=area_id)


def _enforcer(policy: Policy, entities: dict[str, EntityInfo] | None = None) -> PolicyEnforcer:
    return PolicyEnforcer(policy, EntityRegistry(entities or {}))


class TestPriorityRules:
    """Test the entity_ids > device_ids > area_ids > domains > all priority chain."""

    def test_default_deny(self):
        enforcer = _enforcer(Policy())
        assert not enforcer.is_allowed("light.kitchen", Action.READ)
        assert not enforcer.is_allowed("light.kitchen", Action.CONTROL)

    def test_all_allows_read(self):
        enforcer = _enforcer(Policy(all=AccessRule(read=True)))
        assert enforcer.is_allowed("light.kitchen", Action.READ)
        assert not enforcer.is_allowed("light.kitchen", Action.CONTROL)

    def test_domain_overrides_all(self):
        enforcer = _enforcer(
            Policy(all=AccessRule(read=True, control=False), domains={"light": AccessRule(read=True, control=True)}),
            {"light.kitchen": _info("light.kitchen"), "switch.pump": _info("switch.pump")},
        )
        assert enforcer.is_allowed("light.kitchen", Action.CONTROL)
        # switch domain falls back to all
        assert not enforcer.is_allowed("switch.pump", Action.CONTROL)

    def test_area_overrides_domain(self):
        enforcer = _enforcer(
            Policy(
                domains={"light": AccessRule(read=True, control=False)},
                area_ids={"bedroom": AccessRule(read=True, control=True)},
            ),
            {"light.bedroom_lamp": _info("light.bedroom_lamp", area_id="bedroom")},
        )
        assert enforcer.is_allowed("light.bedroom_lamp", Action.CONTROL)

    def test_device_overrides_area(self):
        enforcer = _enforcer(
            Policy(
                area_ids={"bedroom": AccessRule(read=True, control=True)},
                device_ids={"dev_123": AccessRule(read=True, control=False)},
            ),
            {"light.bedroom_lamp": _info("light.bedroom_lamp", device_id="dev_123", area_id="bedroom")},
        )
        assert not enforcer.is_allowed("light.bedroom_lamp", Action.CONTROL)

    def test_entity_overrides_device(self):
        enforcer = _enforcer(
            Policy(
                device_ids={"dev_123": AccessRule(read=True, control=False)},
                entity_ids={"light.special": AccessRule(read=True, control=True)},
            ),
            {"light.special": _info("light.special", device_id="dev_123")},
        )
        assert enforcer.is_allowed("light.special", Action.CONTROL)

    def test_entity_override_denies_even_when_device_allows(self):
        enforcer = _enforcer(
            Policy(
                device_ids={"dev_123": AccessRule(read=True, control=True)},
                entity_ids={"light.dangerous": AccessRule(read=True, control=False)},
            ),
            {"light.dangerous": _info("light.dangerous", device_id="dev_123")},
        )
        assert not enforcer.is_allowed("light.dangerous", Action.CONTROL)


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
        enforcer = _enforcer(
            Policy(entity_ids={"light.allowed": AccessRule(read=True), "light.denied": AccessRule(read=False)}),
            {
                "light.allowed": _info("light.allowed"),
                "light.denied": _info("light.denied"),
                "light.unknown": _info("light.unknown"),
            },
        )
        assert enforcer.readable_entities(["light.allowed", "light.denied", "light.unknown"]) == {"light.allowed"}

    def test_readable_entities_unknown_uses_fallback(self):
        enforcer = _enforcer(Policy(all=AccessRule(read=True)))
        assert enforcer.readable_entities(["sensor.temp"]) == {"sensor.temp"}

    def test_require_read_allowed(self):
        enforcer = _enforcer(Policy(all=AccessRule(read=True)))
        enforcer.require_read("sensor.temp")  # should not raise

    def test_require_read_denied(self):
        enforcer = _enforcer(Policy())
        with pytest.raises(AccessDeniedError) as exc_info:
            enforcer.require_read("sensor.temp")
        assert "sensor.temp" in exc_info.value.entity_ids

    def test_require_control_allowed(self):
        enforcer = _enforcer(Policy(all=AccessRule(control=True)))
        enforcer.require_control(["light.a", "light.b"])  # should not raise

    def test_require_control_denied(self):
        enforcer = _enforcer(
            Policy(entity_ids={"light.ok": AccessRule(control=True), "light.no": AccessRule(control=False)}),
            {"light.ok": _info("light.ok"), "light.no": _info("light.no")},
        )
        with pytest.raises(AccessDeniedError) as exc_info:
            enforcer.require_control(["light.ok", "light.no"])
        assert exc_info.value.entity_ids == ["light.no"]

    def test_resolve_targets(self):
        enforcer = _enforcer(
            Policy(),
            {
                "light.a": _info("light.a", device_id="dev_1", area_id="kitchen"),
                "light.b": _info("light.b", device_id="dev_1"),
                "switch.c": _info("switch.c", area_id="kitchen"),
            },
        )
        targets = enforcer.resolve_targets(entity_ids=["sensor.direct"], device_ids=["dev_1"], area_ids=["kitchen"])
        assert "sensor.direct" in targets
        assert "light.a" in targets  # via device
        assert "light.b" in targets  # via device
        assert "switch.c" in targets  # via area


if __name__ == "__main__":
    pytest_bazel.main()
