"""What the sandbox Actions do with the caller the request names, and what they refuse.

The inventory is a fake rather than a fake API server: these pin the executor's own decisions --
whose sandbox it acts on, which failures become a reason the agent can act on, and which are not
its to answer -- and the Kubernetes wire is the inventory's own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import JsonValue

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionState
from agentplane.action_service.sandbox.actions import SandboxAction
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding
from agentplane.action_service.sandbox.executor import SandboxExecutor
from agentplane.action_service.sandbox.inventory import ForeignSandboxError, SandboxActionError
from agentplane.action_service.sandbox.models import READY_CONDITION, SandboxCondition, SandboxInfo
from agentplane.action_service.service import ExecutionOutcomeUnknownError
from agentplane.subjects import ServiceAccountRef
from mcp_infra.exec.kubernetes import CommandResult, PodExecError
from mcp_infra.exec.models import Exited

NAMESPACE = "agentplane-test"
CALLER = ServiceAccountRef(namespace=NAMESPACE, name="caller-one")
OTHER = ServiceAccountRef(namespace=NAMESPACE, name="caller-two")
ELSEWHERE = ServiceAccountRef(namespace="somewhere-else", name="caller-one")

TEMPLATE = "test-template"
EXPIRES_AT = datetime(2026, 9, 24, 20, 0, tzinfo=UTC)
BINDING = SandboxExecutorBinding(description="test sandboxes", namespace=NAMESPACE, templates={TEMPLATE})
TEMPLATE_SPEC: dict[str, JsonValue] = {
    "podTemplate": {"spec": {"containers": [{"name": "workspace", "image": "test-image:unset"}]}}
}


def _ready() -> SandboxCondition:
    return SandboxCondition(type=READY_CONDITION, status="True", reason="DependenciesReady", message="Pod is Ready")


def _released() -> asyncio.Event:
    """Commands return immediately unless a test holds one open to watch the lease under it."""
    event = asyncio.Event()
    event.set()
    return event


@dataclass
class FakeInventory:
    """Records who asked for what; every call about a box carries the caller the executor resolved."""

    callers: list[ServiceAccountRef] = field(default_factory=list)
    raises: Exception | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=_released)

    def _record(self, caller: ServiceAccountRef) -> None:
        self.callers.append(caller)
        if self.raises is not None:
            raise self.raises

    async def create(self, caller: ServiceAccountRef, name: str, template: str) -> SandboxInfo:
        self._record(caller)
        return SandboxInfo(name=name, conditions=[_ready()], template=template, expires_at=EXPIRES_AT)

    async def get_template(self, name: str) -> dict[str, JsonValue]:
        return {"metadata": {"name": name}, "spec": TEMPLATE_SPEC}

    async def get(self, caller: ServiceAccountRef, name: str) -> SandboxInfo:
        self._record(caller)
        return SandboxInfo(name=name, conditions=[_ready()], template=TEMPLATE, expires_at=EXPIRES_AT)

    async def list(self, caller: ServiceAccountRef) -> list[SandboxInfo]:
        self._record(caller)
        return [SandboxInfo(name="one", conditions=[_ready()], template=TEMPLATE, expires_at=EXPIRES_AT)]

    async def dispose(self, caller: ServiceAccountRef, name: str) -> bool:
        self._record(caller)
        return True

    async def execute(self, caller: ServiceAccountRef, name: str, **kwargs: object) -> CommandResult:
        self._record(caller)
        self.started.set()
        await self.release.wait()
        return CommandResult(exit=Exited(exit_code=3), stdout="out", stderr="err", duration_seconds=0.5)


def _request(action: str, arguments: dict[str, JsonValue], caller: ServiceAccountRef = CALLER) -> ExecutionRequest:
    return ExecutionRequest(
        request_id=uuid4(),
        action=ActionIdentity(group="sandbox", name=action),
        arguments=arguments,
        origin={},
        correlation={},
        caller=caller,
    )


class _Lease:
    """A renewal window far shorter than the command, so an attempt that stopped renewing would
    lapse here exactly as it lapses in Postgres."""

    renewal_interval = timedelta(milliseconds=1)

    def __init__(self) -> None:
        self.owned = True
        self.renewals = 0
        self.renewed_past_window = asyncio.Event()

    async def heartbeat(self) -> bool:
        self.renewals += 1
        # Past the first window, which is the point a non-renewing executor is swept at.
        if self.renewals >= 3:
            self.renewed_past_window.set()
        return self.owned


LEASE: ExecutionLease = _Lease()

EXEC_ARGS: dict[str, JsonValue] = {
    "name": "box",
    "script": "git clone http://git.test.invalid/deep-history",
    "timeout_seconds": 600,
    "max_output_bytes": 1000,
}


@pytest.fixture
def inventory() -> FakeInventory:
    return FakeInventory()


@pytest.fixture
def executor(inventory: FakeInventory) -> SandboxExecutor:
    return SandboxExecutor(BINDING, inventory)  # type: ignore[arg-type]


async def test_every_action_acts_as_the_request_caller(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """The identity comes from the authenticated request and never from an argument, so a caller
    cannot reach another account's boxes by asking for them."""
    await executor.execute(_request(SandboxAction.CREATE, {"name": "box", "template": TEMPLATE}), LEASE)
    await executor.execute(_request(SandboxAction.GET, {"name": "box"}), LEASE)
    await executor.execute(_request(SandboxAction.LIST, {}), LEASE)
    await executor.execute(_request(SandboxAction.DISPOSE, {"name": "box"}), LEASE)
    await executor.execute(_request(SandboxAction.CREATE, {"name": "box", "template": TEMPLATE}, caller=OTHER), LEASE)
    assert inventory.callers == [CALLER, CALLER, CALLER, CALLER, OTHER]


async def test_an_account_named_in_arguments_is_not_read(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """`extra="forbid"` is what keeps the argument schema from carrying an identity at all, so an
    attempt to name one is refused rather than quietly ignored."""
    result = await executor.execute(
        _request(
            SandboxAction.CREATE, {"name": "box", "template": TEMPLATE, "caller": OTHER.name, "namespace": NAMESPACE}
        ),
        LEASE,
    )
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "invalid_arguments"
    assert inventory.callers == []


async def test_a_caller_from_another_namespace_is_refused(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """A Pod runs as an account in its own namespace or not at all, so a caller from elsewhere
    cannot be given a sandbox that is it -- and must not be given one that is somebody else."""
    result = await executor.execute(
        _request(SandboxAction.CREATE, {"name": "box", "template": TEMPLATE}, caller=ELSEWHERE), LEASE
    )
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "caller_not_local"
    assert inventory.callers == []


async def test_a_nonzero_exit_is_a_result(executor: SandboxExecutor) -> None:
    """A command that ran and failed is an answer, not a failed Execution: the Action did what it
    was asked. Only the Action being unable to run at all is a failure."""
    result = await executor.execute(_request(SandboxAction.EXEC, {**EXEC_ARGS, "script": "false"}), LEASE)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.result == {
        "exit": {"kind": "exited", "exit_code": 3},
        "stdout": "out",
        "stderr": "err",
        "duration_seconds": 0.5,
    }


async def test_a_command_outliving_one_lease_window_keeps_it_renewed(
    executor: SandboxExecutor, inventory: FakeInventory
) -> None:
    """A clone deep enough to take minutes is an ordinary command here. Without renewal the sweep
    marks the attempt `execution_unknown` while the script is still running, so the caller is told
    the outcome is unknowable for a command that went on to succeed."""
    inventory.release.clear()
    lease = _Lease()
    running = asyncio.create_task(executor.execute(_request(SandboxAction.EXEC, EXEC_ARGS), lease))
    async with asyncio.timeout(5):
        await lease.renewed_past_window.wait()
        inventory.release.set()
        result = await running
    assert result.state is ExecutionState.SUCCEEDED


async def test_a_lease_lost_mid_command_stops_the_wait(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """The script keeps running in the Pod, so its outcome is no longer this executor's to report:
    an unknown outcome the service records as such, never a result and never a replay."""
    inventory.release.clear()
    lease = _Lease()
    running = asyncio.create_task(executor.execute(_request(SandboxAction.EXEC, EXEC_ARGS), lease))
    async with asyncio.timeout(5):
        await inventory.started.wait()
        lease.owned = False
        with pytest.raises(ExecutionOutcomeUnknownError, match="lease"):
            await running
    assert not any(task.get_name() == "action-execution-renewal" for task in asyncio.all_tasks())


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (ForeignSandboxError("not yours"), "sandbox_not_yours"),
        (SandboxActionError("no such sandbox"), "sandbox_unavailable"),
        (PodExecError("handshake refused"), "exec_failed"),
    ],
)
async def test_a_refusal_becomes_a_reason_the_caller_can_act_on(
    executor: SandboxExecutor, inventory: FakeInventory, raised: Exception, kind: str
) -> None:
    inventory.raises = raised
    result = await executor.execute(_request(SandboxAction.GET, {"name": "box"}), LEASE)
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == kind


async def test_an_unexpected_failure_is_not_swallowed(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """Only outcomes this executor can characterise become a failed Execution; anything else is the
    service's to treat as an unknown outcome rather than a call that definitely did nothing."""
    inventory.raises = RuntimeError("the API server went away mid-delete")
    with pytest.raises(RuntimeError):
        await executor.execute(_request(SandboxAction.DISPOSE, {"name": "box"}), LEASE)


async def test_a_template_comes_back_as_the_object_itself(executor: SandboxExecutor) -> None:
    """Not a summary wrapped around it: the agent reads the Kubernetes object it already knows."""
    result = await executor.execute(_request(SandboxAction.GET_TEMPLATE, {"template": TEMPLATE}), LEASE)
    assert result.state is ExecutionState.SUCCEEDED
    assert result.result == {"metadata": {"name": TEMPLATE}, "spec": TEMPLATE_SPEC}


if __name__ == "__main__":
    pytest_bazel.main()
