"""Own PolicySets and PolicyBindings created by a system-test case."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any
from uuid import uuid4

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.action_service.policies.resources import ACTION_RULE_TYPE, ACTIONS_READY_CONDITION
from agentplane.crd_group import GROUP, VERSION
from agentplane.environments.v1 import environment_service_pb2
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.system_tests.driver.cleanup import finish_cleanup
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.tracing import tracer

# A status write follows the informer's next watch event; the bound covers a relist after a
# dropped watch, not a healthy round trip.
POLICY_READY_SECONDS = 120.0


class PolicyObjectNotReadyError(Exception):
    """The Action Service has not yet judged the binding's current generation."""


class PolicyBindingPresentError(Exception):
    """The binding, or the Action Service's view of it, is still there."""


@dataclass
class PolicyObjects:
    """Policy resources written with this test's kubeconfig and removed when its scope ends."""

    cluster: Cluster
    created: list[tuple[str, str]] = field(default_factory=list)

    async def __aenter__(self) -> PolicyObjects:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> bool:
        del traceback
        with tracer.start_as_current_span("system_test.mcp.policy_cleanup"):
            (await finish_cleanup(self._delete_all())).propagate(exc, note="Policy resource cleanup also failed")
        return False

    async def _create(self, kind: str, plural: str, name: str, spec: dict[str, Any]) -> str:
        body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": kind, "metadata": {"name": name}, "spec": spec}
        await self.cluster.kubectl("create", "-f", "-", body=body)
        self.created.append((plural, name))
        return name

    async def create_policy_set(self, name: str, auto_approve: list[dict[str, Any]]) -> str:
        rules = [{"type": ACTION_RULE_TYPE, "policy": policy} for policy in auto_approve]
        return await self._create("PolicySet", POLICY_SETS_PLURAL, name, {"rules": rules})

    async def create_binding(
        self, name: str, *, sandbox: environment_service_pb2.EnvironmentDescriptor, policy_sets: list[str]
    ) -> str:
        subject = {"identity": {"name": sandbox.identity_name}}
        return await self._create(
            "PolicyBinding", POLICY_BINDINGS_PLURAL, name, {"subject": subject, "policySets": policy_sets}
        )

    async def ensure_basic_binding(self, sandbox: environment_service_pb2.EnvironmentDescriptor) -> None:
        inventory = json.loads(await self.cluster.kubectl("get", POLICY_BINDINGS_PLURAL, "-o", "json"))
        names = [
            binding["metadata"]["name"]
            for binding in inventory["items"]
            if binding.get("spec", {}).get("subject", {}).get("identity", {}).get("name") == sandbox.identity_name
            and "basic-identity" in binding.get("spec", {}).get("policySets", [])
        ]
        if not names:
            names = [
                await self.create_binding(
                    f"{sandbox.target.name}-delegation-basic", sandbox=sandbox, policy_sets=["basic-identity"]
                )
            ]
        for name in names:
            await self.wait_actions_ready(name)

    async def bind(
        self, sandbox: environment_service_pb2.EnvironmentDescriptor, name: str, auto_approve: list[dict[str, Any]]
    ) -> tuple[str, str]:
        """Bind one set to `sandbox`'s Identity and wait for ActionsReady."""
        suffix = uuid4().hex[:8]
        set_name = await self.create_policy_set(f"systest-{name}-{suffix}", auto_approve)
        binding_name = await self.create_binding(
            f"{sandbox.target.name}-{name}-{suffix}", sandbox=sandbox, policy_sets=[set_name]
        )
        await self.wait_actions_ready(binding_name)
        return set_name, binding_name

    async def expire_binding(self, name: str) -> None:
        """Set `expiresAt` in the past and wait until the Policy Controller deletes the binding."""
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        await self.cluster.kubectl(
            "patch", POLICY_BINDINGS_PLURAL, name, "--type=merge", "-p", json.dumps({"spec": {"expiresAt": past}})
        )
        # Yield during policy propagation so the other harness can continue its scenario.
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(POLICY_READY_SECONDS),
            wait=wait_fixed(1),
            retry=retry_if_exception_type(PolicyBindingPresentError),
            reraise=True,
        ):
            with attempt:
                if await self.cluster.kubectl("get", POLICY_BINDINGS_PLURAL, name, "--ignore-not-found", "-o", "name"):
                    raise PolicyBindingPresentError(f"{POLICY_BINDINGS_PLURAL}/{name}")

    async def wait_actions_ready(self, name: str) -> None:
        """Wait until ActionsReady is True for the binding's current generation."""
        with tracer.start_as_current_span("system_test.mcp.policy_ready"):
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(POLICY_READY_SECONDS),
                wait=wait_fixed(1),
                retry=retry_if_exception_type(PolicyObjectNotReadyError),
                reraise=True,
            ):
                with attempt:
                    obj = json.loads(await self.cluster.kubectl("get", POLICY_BINDINGS_PLURAL, name, "-o", "json"))
                    conditions = obj.get("status", {}).get("conditions", [])
                    ready = [c for c in conditions if c["type"] == ACTIONS_READY_CONDITION]
                    generation = obj["metadata"]["generation"]
                    if not ready or ready[0].get("observedGeneration") != generation or ready[0]["status"] != "True":
                        # False is also how a set not yet seen reads, so only the deadline makes it final.
                        raise PolicyObjectNotReadyError(
                            f"{POLICY_BINDINGS_PLURAL}/{name} generation {generation}: {ready}"
                        )

    async def _delete_all(self) -> None:
        failures: list[BaseException] = []
        for plural, name in reversed(self.created):
            try:
                await self.cluster.kubectl("delete", plural, name, "--ignore-not-found")
            except BaseException as error:
                failures.append(error)
        if failures:
            raise BaseExceptionGroup("Policy resource cleanup failed", failures)
