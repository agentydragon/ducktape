"""What the sandbox Actions do with the caller the request names, and what they refuse.

The inventory is a fake rather than a fake API server: these pin the executor's own decisions --
whose sandbox it acts on, which failures become a reason the agent can act on, and which are not
its to answer -- and the Kubernetes wire is the inventory's own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import JsonValue

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.models import ExecutionLease, ExecutionRequest, ExecutionState
from agentplane.action_service.sandbox_executor import SandboxAction, SandboxExecutor, actions
from agentplane.action_service.service import ExecutionOutcomeUnknownError
from agentplane.sandbox_actions.binding import SandboxExecutorBinding
from agentplane.sandbox_actions.inventory import ForeignSandboxError, SandboxActionError
from agentplane.sandbox_actions.models import READY_CONDITION, SandboxCondition, SandboxInfo
from agentplane.subjects import ServiceAccountRef
from mcp_infra.exec.kubernetes import CommandResult, PodExecError
from mcp_infra.exec.models import Exited

NAMESPACE = "agentplane-test"
CALLER = ServiceAccountRef(namespace=NAMESPACE, name="caller-one")
OTHER = ServiceAccountRef(namespace=NAMESPACE, name="caller-two")
ELSEWHERE = ServiceAccountRef(namespace="somewhere-else", name="caller-one")

BINDING = SandboxExecutorBinding(
    description="test sandboxes", namespace=NAMESPACE, templates={"test-template"}, default_template="test-template"
)


def _ready() -> SandboxCondition:
    return SandboxCondition(type=READY_CONDITION, status="True", reason="DependenciesReady", message="Pod is Ready")


def _released() -> asyncio.Event:
    """Commands return immediately unless a test holds one open to watch the lease under it."""
    event = asyncio.Event()
    event.set()
    return event


@dataclass
class FakeInventory:
    """Records who asked for what; every call carries the caller the executor resolved."""

    callers: list[ServiceAccountRef] = field(default_factory=list)
    raises: Exception | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=_released)

    def _record(self, caller: ServiceAccountRef) -> None:
        self.callers.append(caller)
        if self.raises is not None:
            raise self.raises

    async def create(self, caller: ServiceAccountRef, name: str, template_name: str | None) -> SandboxInfo:
        self._record(caller)
        return SandboxInfo(name=name, conditions=[_ready()], template=template_name or BINDING.default_template)

    async def info(self, caller: ServiceAccountRef, name: str) -> SandboxInfo:
        self._record(caller)
        return SandboxInfo(name=name, conditions=[_ready()], template=BINDING.default_template)

    async def list(self, caller: ServiceAccountRef) -> list[SandboxInfo]:
        self._record(caller)
        return [SandboxInfo(name="one", conditions=[_ready()], template=BINDING.default_template)]

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
    await executor.execute(_request(SandboxAction.CREATE, {"name": "box"}), LEASE)
    await executor.execute(_request(SandboxAction.INFO, {"name": "box"}), LEASE)
    await executor.execute(_request(SandboxAction.LIST, {}), LEASE)
    await executor.execute(_request(SandboxAction.DISPOSE, {"name": "box"}), LEASE)
    await executor.execute(_request(SandboxAction.CREATE, {"name": "box"}, caller=OTHER), LEASE)
    assert inventory.callers == [CALLER, CALLER, CALLER, CALLER, OTHER]


async def test_an_account_named_in_arguments_is_not_read(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """`extra="forbid"` is what keeps the argument schema from carrying an identity at all, so an
    attempt to name one is refused rather than quietly ignored."""
    result = await executor.execute(
        _request(SandboxAction.CREATE, {"name": "box", "caller": OTHER.name, "namespace": NAMESPACE}), LEASE
    )
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == "invalid_arguments"
    assert inventory.callers == []


async def test_a_caller_from_another_namespace_is_refused(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """A Pod runs as an account in its own namespace or not at all, so a caller from elsewhere
    cannot be given a sandbox that is it -- and must not be given one that is somebody else."""
    result = await executor.execute(_request(SandboxAction.CREATE, {"name": "box"}, caller=ELSEWHERE), LEASE)
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
    result = await executor.execute(_request(SandboxAction.INFO, {"name": "box"}), LEASE)
    assert result.state is ExecutionState.FAILED
    assert result.error is not None
    assert result.error["kind"] == kind


async def test_an_unexpected_failure_is_not_swallowed(executor: SandboxExecutor, inventory: FakeInventory) -> None:
    """Only outcomes this executor can characterise become a failed Execution; anything else is the
    service's to treat as an unknown outcome rather than a call that definitely did nothing."""
    inventory.raises = RuntimeError("the API server went away mid-delete")
    with pytest.raises(RuntimeError):
        await executor.execute(_request(SandboxAction.DISPOSE, {"name": "box"}), LEASE)


def test_the_offered_actions_name_the_offered_templates() -> None:
    """The offered templates are deployment configuration an agent cannot otherwise see, and naming
    one that is not offered is the likeliest way to get create wrong."""
    offered = actions(BINDING, {"test-template": "the test box"})
    assert set(offered) == set(SandboxAction)
    assert '"test-template": "the test box"' in offered[SandboxAction.CREATE].description
    assert offered[SandboxAction.EXEC].input_schema["additionalProperties"] is False


if __name__ == "__main__":
    pytest_bazel.main()
