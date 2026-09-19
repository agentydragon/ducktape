"""How the inventory reads a Sandbox off the API server, and what it does with what it finds.

These run against the Agent Sandbox CRD's real shape — the controller publishes readiness as a
`Ready` condition and the backing Pod's name as an annotation, and neither is discoverable from
the Pod itself. The first version of this module searched for the Pod by a label the controller
does not write, so `pod_name` was always absent and `exec` could never run; nothing caught it
because the executor's tests fake this class out entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiException

from mcp_infra.exec.kubernetes import CommandResult
from mcp_infra.exec.models import Exited
from x.agentplane.sandbox_actions.binding import SandboxEnvironment, SandboxExecutorBinding
from x.agentplane.sandbox_actions.inventory import POD_NAME_ANNOTATION, SandboxActionError, SandboxInventory
from x.agentplane.sandbox_actions.models import SandboxState
from x.agentplane.subjects import ServiceAccountRef

NAMESPACE = "agentplane-test"
CALLER = ServiceAccountRef(namespace=NAMESPACE, name="caller-one")
# `_object_name` composes it this way; spelled out so a change to that scheme fails here loudly.
OBJECT_NAME = "caller-one-box"

BINDING = SandboxExecutorBinding(
    description="test sandboxes",
    namespace=NAMESPACE,
    environments={
        "default": SandboxEnvironment(
            template="test-template", container="workspace", default_cwd="/workspace", description="the test box"
        )
    },
    default_environment="default",
)


def _sandbox(*, ready: bool, pod_annotation: str | None, reason: str | None = None) -> dict[str, Any]:
    """One Sandbox as the API server returns it."""
    condition = {"type": "Ready", "status": "True" if ready else "False"}
    if reason is not None:
        condition["message"] = reason
    return {
        "metadata": {
            "name": OBJECT_NAME,
            "creationTimestamp": "2026-09-19T04:15:32Z",
            "labels": {
                "sandbox-actions.agentplane.allegedly.works/managed": "true",
                "sandbox-actions.agentplane.allegedly.works/caller": CALLER.name,
                "sandbox-actions.agentplane.allegedly.works/caller-namespace": CALLER.namespace,
                "sandbox-actions.agentplane.allegedly.works/name": "box",
                "sandbox-actions.agentplane.allegedly.works/environment": "default",
            },
            **({"annotations": {POD_NAME_ANNOTATION: pod_annotation}} if pod_annotation is not None else {}),
        },
        "status": {"conditions": [condition]},
    }


@dataclass
class FakeCustomObjects:
    sandbox: dict[str, Any]

    async def get_namespaced_custom_object(self, *args: object) -> dict[str, Any]:
        return self.sandbox


@dataclass
class FakeCoreV1:
    """Holds the Pods that exist; anything else 404s the way the API server does."""

    pods: set[str] = field(default_factory=set)
    read: list[str] = field(default_factory=list)

    async def read_namespaced_pod(self, name: str, namespace: str) -> object:
        self.read.append(name)
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        return object()


@dataclass
class FakeExecRunner:
    ran_against: list[str] = field(default_factory=list)

    async def run(self, *, pod_name: str, **kwargs: object) -> CommandResult:
        self.ran_against.append(pod_name)
        return CommandResult(exit=Exited(exit_code=0), stdout="", stderr="", duration_seconds=0.1)


def _inventory(sandbox: dict[str, Any], core_v1: FakeCoreV1, runner: FakeExecRunner) -> SandboxInventory:
    return SandboxInventory(
        BINDING,
        custom_objects=FakeCustomObjects(sandbox),  # type: ignore[arg-type]
        core_v1=core_v1,  # type: ignore[arg-type]
        exec_runner=runner,  # `ExecRunner` is a Protocol, so this one needs no ignore.
    )


async def test_the_pod_name_comes_from_the_controllers_annotation() -> None:
    """The Pod carries nothing tying it back to its Sandbox, so this annotation is the only link."""
    core_v1 = FakeCoreV1(pods={"sandbox-pod-abc123"})
    inventory = _inventory(_sandbox(ready=True, pod_annotation="sandbox-pod-abc123"), core_v1, FakeExecRunner())
    info = await inventory.info(CALLER, "box")
    assert info.state is SandboxState.READY
    assert info.pod_name == "sandbox-pod-abc123"


async def test_an_unannotated_sandbox_falls_back_to_its_own_name() -> None:
    """The controller names the Pod after the Sandbox when it publishes no annotation."""
    core_v1 = FakeCoreV1(pods={OBJECT_NAME})
    inventory = _inventory(_sandbox(ready=True, pod_annotation=None), core_v1, FakeExecRunner())
    assert (await inventory.info(CALLER, "box")).pod_name == OBJECT_NAME


async def test_a_ready_sandbox_whose_pod_is_gone_reports_no_pod() -> None:
    """A Sandbox that has been Ready still names a Pod an eviction has since taken away, so the
    annotation is confirmed against the API server rather than trusted."""
    core_v1 = FakeCoreV1(pods=set())
    inventory = _inventory(_sandbox(ready=True, pod_annotation="sandbox-pod-abc123"), core_v1, FakeExecRunner())
    info = await inventory.info(CALLER, "box")
    assert info.state is SandboxState.READY
    assert info.pod_name is None


async def test_a_not_ready_sandbox_is_not_searched_for_a_pod() -> None:
    """Readiness is the controller's answer; there is nothing to exec into before it says so."""
    core_v1 = FakeCoreV1(pods={OBJECT_NAME})
    inventory = _inventory(
        _sandbox(ready=False, pod_annotation=None, reason="Pod exists with phase: Pending"), core_v1, FakeExecRunner()
    )
    info = await inventory.info(CALLER, "box")
    assert (info.state, info.reason) == (SandboxState.NOT_READY, "Pod exists with phase: Pending")
    assert info.pod_name is None
    assert core_v1.read == []


async def test_exec_runs_against_the_pod_the_controller_named() -> None:
    runner = FakeExecRunner()
    inventory = _inventory(
        _sandbox(ready=True, pod_annotation="sandbox-pod-abc123"), FakeCoreV1(pods={"sandbox-pod-abc123"}), runner
    )
    await inventory.execute(CALLER, "box", script="true", cwd=None, timeout_seconds=5, max_output_bytes=100)
    assert runner.ran_against == ["sandbox-pod-abc123"]


@pytest.mark.parametrize(
    ("ready", "expected"),
    [
        (False, "is not ready"),
        # The distinction the first version lost: it reported a ready box as unable to run with
        # `reason=unknown`, which reads as a contradiction to the agent deciding whether to poll.
        (True, "has no running Pod"),
    ],
)
async def test_a_box_with_no_reachable_pod_says_which_of_the_two_it_is(ready: bool, expected: str) -> None:
    inventory = _inventory(
        _sandbox(ready=ready, pod_annotation="sandbox-pod-abc123", reason="Pod exists with phase: Pending"),
        FakeCoreV1(pods=set()),
        FakeExecRunner(),
    )
    with pytest.raises(SandboxActionError, match=expected):
        await inventory.execute(CALLER, "box", script="true", cwd=None, timeout_seconds=5, max_output_bytes=100)


if __name__ == "__main__":
    pytest_bazel.main()
