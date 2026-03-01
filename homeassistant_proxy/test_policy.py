"""Tests for the policy evaluation engine."""

import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_bazel

from homeassistant_proxy.config import AccessRule, Action, EntityInfo, Policy
from homeassistant_proxy.policy import AccessDeniedError, PolicyEnforcer


def _info(entity_id: str, device_id: str | None = None, area_id: str | None = None) -> EntityInfo:
    return EntityInfo(entity_id=entity_id, device_id=device_id, area_id=area_id)


def _enforcer(entities: dict[str, EntityInfo] | None = None) -> PolicyEnforcer:
    enforcer = PolicyEnforcer("http://unused", "unused-token")
    if entities is not None:
        enforcer._entities = entities
        enforcer._entities_time = time.monotonic()
    else:
        enforcer._entities = {}
        enforcer._entities_time = time.monotonic()
    return enforcer


class TestPriorityRules:
    """Test the entity_ids > device_ids > area_ids > domains > all priority chain."""

    async def test_default_deny(self):
        enforcer = _enforcer()
        assert not await enforcer.is_allowed("light.kitchen", Action.READ, Policy())
        assert not await enforcer.is_allowed("light.kitchen", Action.CONTROL, Policy())

    async def test_all_allows_read(self):
        policy = Policy(all=AccessRule(read=True))
        enforcer = _enforcer()
        assert await enforcer.is_allowed("light.kitchen", Action.READ, policy)
        assert not await enforcer.is_allowed("light.kitchen", Action.CONTROL, policy)

    async def test_domain_overrides_all(self):
        policy = Policy(
            all=AccessRule(read=True, control=False), domains={"light": AccessRule(read=True, control=True)}
        )
        enforcer = _enforcer({"light.kitchen": _info("light.kitchen"), "switch.pump": _info("switch.pump")})
        assert await enforcer.is_allowed("light.kitchen", Action.CONTROL, policy)
        # switch domain falls back to all
        assert not await enforcer.is_allowed("switch.pump", Action.CONTROL, policy)

    async def test_area_overrides_domain(self):
        policy = Policy(
            domains={"light": AccessRule(read=True, control=False)},
            area_ids={"bedroom": AccessRule(read=True, control=True)},
        )
        enforcer = _enforcer({"light.bedroom_lamp": _info("light.bedroom_lamp", area_id="bedroom")})
        assert await enforcer.is_allowed("light.bedroom_lamp", Action.CONTROL, policy)

    async def test_device_overrides_area(self):
        policy = Policy(
            area_ids={"bedroom": AccessRule(read=True, control=True)},
            device_ids={"dev_123": AccessRule(read=True, control=False)},
        )
        enforcer = _enforcer(
            {"light.bedroom_lamp": _info("light.bedroom_lamp", device_id="dev_123", area_id="bedroom")}
        )
        assert not await enforcer.is_allowed("light.bedroom_lamp", Action.CONTROL, policy)

    async def test_entity_overrides_device(self):
        policy = Policy(
            device_ids={"dev_123": AccessRule(read=True, control=False)},
            entity_ids={"light.special": AccessRule(read=True, control=True)},
        )
        enforcer = _enforcer({"light.special": _info("light.special", device_id="dev_123")})
        assert await enforcer.is_allowed("light.special", Action.CONTROL, policy)

    async def test_entity_override_denies_even_when_device_allows(self):
        policy = Policy(
            device_ids={"dev_123": AccessRule(read=True, control=True)},
            entity_ids={"light.dangerous": AccessRule(read=True, control=False)},
        )
        enforcer = _enforcer({"light.dangerous": _info("light.dangerous", device_id="dev_123")})
        assert not await enforcer.is_allowed("light.dangerous", Action.CONTROL, policy)


class TestPolicyEnforcer:
    async def test_readable_entities(self):
        policy = Policy(entity_ids={"light.allowed": AccessRule(read=True), "light.denied": AccessRule(read=False)})
        enforcer = _enforcer(
            {
                "light.allowed": _info("light.allowed"),
                "light.denied": _info("light.denied"),
                "light.unknown": _info("light.unknown"),
            }
        )
        result = await enforcer.readable_entities(["light.allowed", "light.denied", "light.unknown"], policy)
        assert result == {"light.allowed"}

    async def test_readable_entities_unknown_uses_fallback(self):
        policy = Policy(all=AccessRule(read=True))
        enforcer = _enforcer()
        assert await enforcer.readable_entities(["sensor.temp"], policy) == {"sensor.temp"}

    async def test_require_read_allowed(self):
        policy = Policy(all=AccessRule(read=True))
        enforcer = _enforcer()
        await enforcer.require_read("sensor.temp", policy)  # should not raise

    async def test_require_read_denied(self):
        enforcer = _enforcer()
        with pytest.raises(AccessDeniedError) as exc_info:
            await enforcer.require_read("sensor.temp", Policy())
        assert "sensor.temp" in exc_info.value.entity_ids

    async def test_require_control_allowed(self):
        policy = Policy(all=AccessRule(control=True))
        enforcer = _enforcer()
        await enforcer.require_control(["light.a", "light.b"], policy)  # should not raise

    async def test_require_control_denied(self):
        policy = Policy(entity_ids={"light.ok": AccessRule(control=True), "light.no": AccessRule(control=False)})
        enforcer = _enforcer({"light.ok": _info("light.ok"), "light.no": _info("light.no")})
        with pytest.raises(AccessDeniedError) as exc_info:
            await enforcer.require_control(["light.ok", "light.no"], policy)
        assert exc_info.value.entity_ids == ["light.no"]

    async def test_resolve_targets(self):
        enforcer = _enforcer(
            {
                "light.a": _info("light.a", device_id="dev_1", area_id="kitchen"),
                "light.b": _info("light.b", device_id="dev_1"),
                "switch.c": _info("switch.c", area_id="kitchen"),
            }
        )
        targets = await enforcer.resolve_targets(
            entity_ids=["sensor.direct"], device_ids=["dev_1"], area_ids=["kitchen"]
        )
        assert "sensor.direct" in targets
        assert "light.a" in targets  # via device
        assert "light.b" in targets  # via device
        assert "switch.c" in targets  # via area


class TestConnectionLifecycle:
    async def test_stale_cache_on_disconnect(self):
        """When connection is lost, stale cached entities are served."""
        enforcer = PolicyEnforcer("http://unused", "unused-token")
        enforcer._entities = {"light.a": _info("light.a")}
        enforcer._entities_time = 0  # Expired TTL
        with patch.object(enforcer, "_fetch_registry", new_callable=AsyncMock, side_effect=ConnectionError("down")):
            entities = await enforcer._ensure_entities()
        assert "light.a" in entities

    async def test_initial_fetch_failure_raises(self):
        """When no cached entities exist and connection fails, raise."""
        enforcer = PolicyEnforcer("http://unused", "unused-token")
        with (
            patch.object(enforcer, "_fetch_registry", new_callable=AsyncMock, side_effect=ConnectionError("down")),
            pytest.raises(ConnectionError),
        ):
            await enforcer._ensure_entities()

    async def test_start_stop_lifecycle(self):
        """start() creates a task, stop() cancels it."""
        enforcer = PolicyEnforcer("http://unused", "unused-token")
        with patch.object(enforcer, "_connection_loop", new_callable=AsyncMock):
            await enforcer.start()
            assert enforcer._connection_task is not None
            await enforcer.stop()
            assert enforcer._connection_task is None
            assert enforcer._client is None


if __name__ == "__main__":
    pytest_bazel.main()
