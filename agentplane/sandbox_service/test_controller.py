"""The work queue responds to watches, retries failures, and survives missed events."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import pytest_bazel
from kubernetes_asyncio import watch as k8s_watch

from agentplane.proto.sandbox_service_pb2 import Sandbox, TeardownPhase
from agentplane.sandbox_service.controller import SandboxController
from agentplane.sandbox_service.errors import SandboxNotFoundError
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandbox_service.teardown import Teardown
from util.agent_sandbox import SANDBOX_API, SANDBOXES_PLURAL

# gazelle:include_dep @pypi//protobuf


@dataclass
class Inventory:
    namespace: str = "test-ns"
    sandboxes: dict[str, Sandbox] = field(default_factory=dict)
    listed: list[dict[str, Any]] = field(default_factory=list)

    async def sandbox_list(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"metadata": {"resourceVersion": "42"}, "items": self.listed}

    async def get(self, name: str) -> Sandbox:
        if name not in self.sandboxes:
            raise SandboxNotFoundError(name)
        return self.sandboxes[name]


@dataclass
class Bindings:
    ensured: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)

    async def ensure(self, sandbox: Sandbox) -> None:
        self.ensured.append(sandbox.name)

    async def cleanup(self, sandbox: Sandbox) -> None:
        self.cleaned.append(sandbox.name)


@dataclass
class Teardowns:
    advanced: list[str] = field(default_factory=list)

    async def advance(self, sandbox: Sandbox) -> None:
        self.advanced.append(sandbox.name)


@dataclass
class Provisioner:
    teardown: Teardowns = field(default_factory=Teardowns)
    inventory: Inventory = field(default_factory=Inventory)
    bindings: Bindings = field(default_factory=Bindings)
    ensured: list[str] = field(default_factory=list)
    sweeps: int = 0
    fail_once: bool = False
    release: asyncio.Event | None = None

    async def ensure(self, sandbox: Sandbox) -> None:
        self.ensured.append(sandbox.name)
        if self.release is not None:
            await self.release.wait()
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("transient")

    async def reconcile_once(self) -> None:
        self.sweeps += 1


def controller(provisioner: Provisioner, *, resync_seconds: int = 300) -> SandboxController:
    return SandboxController(
        cast(Provisioning, provisioner), cast(Teardown, provisioner.teardown), resync_seconds=resync_seconds
    )


async def _until(predicate: Any) -> None:
    async def wait() -> None:
        while not predicate():  # noqa: ASYNC110 - test waits on unrelated fake callbacks.
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=3)


async def test_watch_lists_from_resource_version_and_requeues_each_current_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisioner = Provisioner()
    provisioner.inventory.listed = [{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}]
    result = controller(provisioner)
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class Watch:
        async def stream(self, *args: object, **kwargs: object) -> Any:
            seen.append((args, kwargs))
            yield {"type": "MODIFIED", "object": {"metadata": {"name": "a"}}}
            yield {"type": "DELETED", "object": {"metadata": {"name": "b"}}}
            yield {"type": "BOOKMARK", "object": {}}

        def stop(self) -> None:
            pass

    monkeypatch.setattr(k8s_watch, "Watch", Watch)
    kind = result._watch._kinds[0]
    await result._watch._cycle(kind)
    assert seen[0][0][1:] == (*SANDBOX_API, provisioner.inventory.namespace, SANDBOXES_PLURAL)
    assert seen[0][1]["resource_version"] == "42"
    assert seen[0][1]["label_selector"] == "agentplane.allegedly.works/managed=true"
    assert list(cast(Any, result._queue)._queue) == ["a", "b", ""]  # one sweep for the deletion
    # A reconnect relists even unchanged objects and notices deletions during watch gaps.
    provisioner.inventory.listed = [{"metadata": {"name": "a"}}]
    await result._watch._cycle(kind)
    assert list(cast(Any, result._queue)._queue) == ["a", "b", ""]
    assert result._seen == {"a"}


async def test_worker_reconciles_current_uid_and_deletion() -> None:
    provisioner = Provisioner()
    result = controller(provisioner)
    provisioner.inventory.sandboxes["a"] = Sandbox(name="a", uid="new")
    provisioner.inventory.sandboxes["b"] = Sandbox(name="b", uid="old", deleting=True)
    provisioner.inventory.sandboxes["c"] = Sandbox(
        name="c", uid="torn-down", teardown=TeardownPhase.TEARDOWN_PHASE_SEALING
    )
    result._apply("a", {"metadata": {"name": "a"}})
    result._apply("b", {"metadata": {"name": "b"}})
    result._apply("c", {"metadata": {"name": "c"}})
    result._apply("gone", {"metadata": {"name": "gone"}})
    worker = asyncio.create_task(result._worker())
    try:
        await asyncio.wait_for(result._queue.join(), 2)
        assert provisioner.bindings.ensured == ["a", "c"]
        assert provisioner.ensured == ["a", "c"]
        assert provisioner.bindings.cleaned == ["b"]
        assert provisioner.teardown.advanced == ["c"]
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_retry_is_per_name_and_new_event_is_not_lost() -> None:
    provisioner = Provisioner(fail_once=True, release=asyncio.Event())
    provisioner.inventory.sandboxes["a"] = Sandbox(name="a", uid="test")
    result = controller(provisioner)
    worker = asyncio.create_task(result._worker())
    try:
        result._apply("a", {"metadata": {"name": "a"}})
        await _until(lambda: provisioner.ensured == ["a"])
        # An update while the first reconcile is blocked must cause a second pass.
        result._apply("a", {"metadata": {"name": "a"}})
        assert provisioner.release is not None
        provisioner.release.set()
        await _until(lambda: result._attempts.get("a") == 1)
        # An event cannot bypass the first failure's backoff (e.g. our own error annotation).
        result._apply("a", {"metadata": {"name": "a"}})
        await asyncio.wait_for(result._queue.join(), 2)
        assert provisioner.ensured == ["a"]
        await _until(lambda: len(provisioner.ensured) == 2)
        await asyncio.wait_for(result._queue.join(), 2)
        assert result._attempts == {}
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        for task in result._retries.values():
            task.cancel()
        await asyncio.gather(*result._retries.values(), return_exceptions=True)


async def test_failed_name_retries_without_another_watch_event() -> None:
    provisioner = Provisioner(fail_once=True)
    provisioner.inventory.sandboxes["a"] = Sandbox(name="a", uid="test")
    result = controller(provisioner)
    worker = asyncio.create_task(result._worker())
    try:
        result._apply("a", {"metadata": {"name": "a"}})
        await _until(lambda: len(provisioner.ensured) >= 2)
        await asyncio.wait_for(result._queue.join(), 2)
        assert provisioner.ensured == ["a", "a"]
        assert result._attempts == {}
        assert result._retries == {}
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


async def test_safety_sweep_runs_when_watch_is_quiet_or_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    provisioner = Provisioner()
    result = controller(provisioner, resync_seconds=1)

    async def unavailable() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(result._watch, "run", unavailable)
    task = asyncio.create_task(result.run())
    try:
        await _until(lambda: provisioner.sweeps >= 1)  # startup recovery
        await asyncio.wait_for(_until(lambda: provisioner.sweeps >= 2), 2)  # missed event repair
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    pytest_bazel.main()
