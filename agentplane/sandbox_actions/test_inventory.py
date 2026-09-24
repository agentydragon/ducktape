"""How the inventory reads a Sandbox off the API server, and what it does with what it finds.

These run against the Agent Sandbox CRD's real shape — the controller publishes readiness as a
`Ready` condition and the backing Pod's name as an annotation, and neither is discoverable from
the Pod itself. The first version of this module searched for the Pod by a label the controller
does not write, so `pod_name` was always absent and `exec` could never run; nothing caught it
because the executor's tests fake this class out entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_bazel
from kubernetes_asyncio.client import ApiException

from agentplane.sandbox_actions.binding import SandboxEnvironment, SandboxExecutorBinding
from agentplane.sandbox_actions.inventory import SandboxActionError, SandboxInventory
from agentplane.sandbox_actions.models import READY_CONDITION
from agentplane.subjects import ServiceAccountRef
from mcp_infra.exec.kubernetes import CommandResult
from mcp_infra.exec.models import Exited
from util.agent_sandbox import POD_NAME_ANNOTATION, TEMPLATES_PLURAL

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
    initial_ttl_seconds=8 * 3600,
    exec_ttl_extension_seconds=2 * 3600,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _sandbox(
    *,
    ready: bool,
    pod_annotation: str | None,
    reason: str | None = None,
    expires_at: datetime = NOW + timedelta(hours=5),
) -> dict[str, Any]:
    """One Sandbox as the API server returns it."""
    condition = {
        "type": READY_CONDITION,
        "status": "True" if ready else "False",
        "reason": "DependenciesReady" if ready else "PodNotReady",
    }
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
        "spec": {"shutdownTime": expires_at.isoformat()},
        "status": {"conditions": [condition]},
    }


@dataclass
class FakeCustomObjects:
    """The one Sandbox, absent until created, and the template `create` stamps it from."""

    sandbox: dict[str, Any] | None
    created: list[dict[str, Any]] = field(default_factory=list)
    patches: list[object] = field(default_factory=list)

    async def get_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str
    ) -> dict[str, Any]:
        if plural == TEMPLATES_PLURAL:
            return {"spec": {"podTemplate": {"spec": {"containers": [{"name": "workspace"}]}}}}
        if self.sandbox is None:
            raise ApiException(status=404, reason="Not Found")
        return self.sandbox

    async def create_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.created.append(body)
        self.sandbox = body
        return body

    async def patch_namespaced_custom_object(
        self, group: str, version: str, namespace: str, plural: str, name: str, body: object, *, _content_type: str
    ) -> object:
        self.patches.append(body)
        return body


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


def _inventory(
    api: dict[str, Any] | FakeCustomObjects, core_v1: FakeCoreV1, runner: FakeExecRunner
) -> SandboxInventory:
    """Over `api` where a test reads back what was written, else over a fake holding just that Sandbox."""
    return SandboxInventory(
        BINDING,
        custom_objects=api if isinstance(api, FakeCustomObjects) else FakeCustomObjects(api),  # type: ignore[arg-type]
        core_v1=core_v1,  # type: ignore[arg-type]
        exec_runner=runner,  # `ExecRunner` is a Protocol, so this one needs no ignore.
        now=lambda: NOW,
    )


async def test_the_pod_name_comes_from_the_controllers_annotation() -> None:
    """The Pod carries nothing tying it back to its Sandbox, so this annotation is the only link."""
    core_v1 = FakeCoreV1(pods={"sandbox-pod-abc123"})
    inventory = _inventory(_sandbox(ready=True, pod_annotation="sandbox-pod-abc123"), core_v1, FakeExecRunner())
    info = await inventory.info(CALLER, "box")
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
    assert info.pod_name is None


async def test_a_not_ready_sandbox_is_not_searched_for_a_pod() -> None:
    """Readiness is the controller's answer; there is nothing to exec into before it says so."""
    core_v1 = FakeCoreV1(pods={OBJECT_NAME})
    inventory = _inventory(
        _sandbox(ready=False, pod_annotation=None, reason="Pod exists with phase: Pending"), core_v1, FakeExecRunner()
    )
    info = await inventory.info(CALLER, "box")
    assert info.pod_name is None
    assert core_v1.read == []


async def test_every_condition_reaches_the_caller_as_the_controller_wrote_it() -> None:
    """A Sandbox publishes more than readiness, and summarising it here would drop the rest.

    `Suspended` is the case that motivates this: a stopped box and a box still coming up are both
    "not Ready", and only its own condition distinguishes them.
    """
    sandbox = _sandbox(ready=False, pod_annotation=None, reason="Pod exists with phase: Pending")
    sandbox["status"]["conditions"].insert(
        0,
        {
            "type": "Suspended",
            "status": "True",
            "reason": "SuspendedByOperator",
            "message": "Sandbox is suspended",
            "lastTransitionTime": "2026-09-19T09:49:30Z",
        },
    )
    info = await _inventory(sandbox, FakeCoreV1(), FakeExecRunner()).info(CALLER, "box")
    suspended, ready = info.conditions
    assert (suspended.type, suspended.status, suspended.reason) == ("Suspended", "True", "SuspendedByOperator")
    assert suspended.last_transition_time is not None
    assert (ready.type, ready.status, ready.message) == (READY_CONDITION, "False", "Pod exists with phase: Pending")


async def test_a_sandbox_with_no_status_yet_reports_no_conditions() -> None:
    """The controller has not written one between the create call and the read that follows it."""
    sandbox = _sandbox(ready=False, pod_annotation=None)
    del sandbox["status"]
    info = await _inventory(sandbox, FakeCoreV1(), FakeExecRunner()).info(CALLER, "box")
    assert info.conditions == []
    assert info.pod_name is None


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


async def test_create_stamps_a_box_the_controller_deletes_after_the_initial_ttl() -> None:
    api = FakeCustomObjects(None)
    info = await _inventory(api, FakeCoreV1(), FakeExecRunner()).create(CALLER, "box", None)
    (body,) = api.created
    assert body["spec"]["shutdownPolicy"] == "Delete"
    assert datetime.fromisoformat(body["spec"]["shutdownTime"]) == NOW + timedelta(hours=8)
    assert info.expires_at == NOW + timedelta(hours=8)


@pytest.mark.parametrize(
    ("expires_in", "pushed_to"),
    [
        # Under the extension: the exec keeps the box two hours past its start.
        (timedelta(minutes=30), NOW + timedelta(hours=2)),
        # Already past it: an exec never shortens a box's life, and does not write at all.
        (timedelta(hours=5), None),
    ],
)
async def test_exec_keeps_the_box_at_least_the_extension_past_its_start(
    expires_in: timedelta, pushed_to: datetime | None
) -> None:
    api = FakeCustomObjects(_sandbox(ready=True, pod_annotation="sandbox-pod-abc123", expires_at=NOW + expires_in))
    runner = FakeExecRunner()
    inventory = _inventory(api, FakeCoreV1(pods={"sandbox-pod-abc123"}), runner)
    await inventory.execute(CALLER, "box", script="true", cwd=None, timeout_seconds=5, max_output_bytes=100)
    assert runner.ran_against == ["sandbox-pod-abc123"]
    assert api.patches == ([] if pushed_to is None else [{"spec": {"shutdownTime": pushed_to.isoformat()}}])


async def test_exec_refuses_an_expired_box_rather_than_reviving_it() -> None:
    """The controller may not have marked it yet, and pushing its time forward would race the
    teardown the controller has already begun."""
    api = FakeCustomObjects(_sandbox(ready=True, pod_annotation="sandbox-pod-abc123", expires_at=NOW))
    runner = FakeExecRunner()
    inventory = _inventory(api, FakeCoreV1(pods={"sandbox-pod-abc123"}), runner)
    with pytest.raises(SandboxActionError, match="has expired"):
        await inventory.execute(CALLER, "box", script="true", cwd=None, timeout_seconds=5, max_output_bytes=100)
    assert (runner.ran_against, api.patches) == ([], [])


if __name__ == "__main__":
    pytest_bazel.main()
