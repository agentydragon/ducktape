"""How the inventory stamps a box's Pod, reads it back off the API server, and what it does with what
it finds."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
import pytest_bazel
from kubernetes_asyncio.client import (
    ApiClient,
    ApiException,
    V1Container,
    V1ObjectMeta,
    V1Pod,
    V1PodCondition,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1PodTemplate,
    V1PodTemplateSpec,
    V1ProjectedVolumeSource,
    V1ServiceAccount,
    V1ServiceAccountTokenProjection,
    V1Volume,
    V1VolumeMount,
    V1VolumeProjection,
)

from agentplane.action_service.sandbox.binding import PodExecutorBinding
from agentplane.action_service.sandbox.inventory import (
    DEFAULT_CONTAINER_ANNOTATION,
    ForeignSandboxError,
    SandboxActionError,
    SandboxInventory,
)
from agentplane.action_service.sandbox.models import READY_CONDITION
from agentplane.subjects import ServiceAccountRef
from mcp_infra.exec.kubernetes import CommandResult
from util.exec.models import Exited

NAMESPACE = "agentplane-test"
CALLER = ServiceAccountRef(namespace=NAMESPACE, name="caller-one")
CALLER_UID = "test-caller-uid"
# `_object_name` composes it this way; spelled out so a change to that scheme fails here loudly.
OBJECT_NAME = "caller-one-box"

BINDING = PodExecutorBinding(
    description="test sandboxes", namespace=NAMESPACE, templates={"test-template": "the test box"}
)


def _template() -> V1PodTemplate:
    """The smallest template a box may come from: the egress sidecar, and no token in the Pod."""
    return V1PodTemplate(
        metadata=V1ObjectMeta(name="test-template", namespace=NAMESPACE),
        template=V1PodTemplateSpec(
            metadata=V1ObjectMeta(labels={"app.kubernetes.io/name": "test-box"}),
            spec=V1PodSpec(
                automount_service_account_token=False,
                containers=[V1Container(name="workspace"), V1Container(name="egress-sidecar")],
            ),
        ),
    )


def _pod(
    *, ready: bool, message: str | None = None, caller: str = CALLER.name, annotations: dict[str, str] | None = None
) -> V1Pod:
    """One box's Pod as the API server returns it."""
    return V1Pod(
        metadata=V1ObjectMeta(
            name=OBJECT_NAME,
            namespace=NAMESPACE,
            creation_timestamp=datetime(2026, 9, 19, 4, 15, 32, tzinfo=UTC),
            annotations=annotations,
            labels={
                "sandbox-actions.agentplane.allegedly.works/managed": "true",
                "sandbox-actions.agentplane.allegedly.works/caller": caller,
                "sandbox-actions.agentplane.allegedly.works/caller-namespace": CALLER.namespace,
                "sandbox-actions.agentplane.allegedly.works/name": "box",
                "sandbox-actions.agentplane.allegedly.works/template": "test-template",
            },
        ),
        spec=V1PodSpec(containers=[V1Container(name="workspace"), V1Container(name="egress-sidecar")]),
        status=V1PodStatus(
            phase="Running" if ready else "Pending",
            conditions=[
                V1PodCondition(
                    type=READY_CONDITION,
                    status="True" if ready else "False",
                    reason=None if ready else "ContainersNotReady",
                    message=message,
                )
            ],
        ),
    )


@dataclass
class FakeCoreV1:
    """Holds the Pods and the PodTemplate that exist; anything else 404s the way the API server does."""

    pods: dict[str, V1Pod] = field(default_factory=dict)
    template: V1PodTemplate = field(default_factory=_template)
    created: list[V1Pod] = field(default_factory=list)
    refuse_create: ApiException | None = None

    async def read_namespaced_pod(self, name: str, namespace: str) -> V1Pod:
        if name not in self.pods:
            raise ApiException(status=404, reason="Not Found")
        return self.pods[name]

    async def read_namespaced_pod_template(self, name: str, namespace: str) -> V1PodTemplate:
        return self.template

    async def read_namespaced_service_account(self, name: str, namespace: str) -> V1ServiceAccount:
        return V1ServiceAccount(metadata=V1ObjectMeta(name=name, namespace=namespace, uid=CALLER_UID))

    async def create_namespaced_pod(self, namespace: str, body: V1Pod) -> None:
        if self.refuse_create is not None:
            raise self.refuse_create
        self.created.append(body)
        self.pods[body.metadata.name] = _pod(ready=False)

    async def list_namespaced_pod(self, namespace: str, *, label_selector: str) -> V1PodList:
        return V1PodList(items=list(self.pods.values()))


@dataclass
class FakeExecRunner:
    ran_against: list[str] = field(default_factory=list)
    ran_in: list[str] = field(default_factory=list)

    async def run(self, *, pod_name: str, container: str, **kwargs: object) -> CommandResult:
        self.ran_against.append(pod_name)
        self.ran_in.append(container)
        return CommandResult(exit=Exited(exit_code=0), stdout="", stderr="", duration_seconds=0.1)


def _inventory(
    core_v1: FakeCoreV1, runner: FakeExecRunner | None = None, api: ApiClient | None = None
) -> SandboxInventory:
    return SandboxInventory(
        BINDING,
        # Only `get_template` serializes; the tests that reach it pass a real client.
        api=api,  # type: ignore[arg-type]
        core_v1=core_v1,  # type: ignore[arg-type]
        exec_runner=runner or FakeExecRunner(),  # `ExecRunner` is a Protocol, so this one needs no ignore.
    )


async def test_a_box_runs_as_its_caller_and_goes_with_its_account() -> None:
    """The template's Pod, as the caller and owned by the caller's ServiceAccount, so deleting a
    Session Sandbox's account takes the boxes its agent made."""
    core_v1 = FakeCoreV1()
    await _inventory(core_v1).create(CALLER, "box", "test-template")
    (pod,) = core_v1.created
    assert pod.spec.service_account_name == CALLER.name
    assert [container.name for container in pod.spec.containers] == ["workspace", "egress-sidecar"]
    (owner,) = pod.metadata.owner_references
    assert (owner.kind, owner.name, owner.uid) == ("ServiceAccount", CALLER.name, CALLER_UID)
    # The template's own labels stay: its network policy selects the box by them.
    assert pod.metadata.labels["app.kubernetes.io/name"] == "test-box"


def _without_sidecar(spec: V1PodSpec) -> None:
    spec.containers = [container for container in spec.containers if container.name != "egress-sidecar"]


def _with_token_automounted(spec: V1PodSpec) -> None:
    spec.automount_service_account_token = True


def _with_token_mounted_in(container: int) -> Callable[[V1PodSpec], None]:
    def edit(spec: V1PodSpec) -> None:
        spec.volumes = [
            V1Volume(
                name="test-token",
                projected=V1ProjectedVolumeSource(
                    sources=[V1VolumeProjection(service_account_token=V1ServiceAccountTokenProjection(path="token"))]
                ),
            )
        ]
        spec.containers[container].volume_mounts = [V1VolumeMount(name="test-token", mount_path="/var/run/test")]

    return edit


@pytest.mark.parametrize(
    ("edit", "refused"),
    [
        (_without_sidecar, True),
        (_with_token_automounted, True),
        (_with_token_mounted_in(0), True),
        # The egress path itself mounts the Pod's tokens; this keeps the refusals from passing vacuously.
        (_with_token_mounted_in(1), False),
    ],
)
async def test_a_template_that_lets_the_workload_escape_the_egress_path_is_refused(
    edit: Callable[[V1PodSpec], None], refused: bool
) -> None:
    core_v1 = FakeCoreV1()
    edit(core_v1.template.template.spec)
    if refused:
        with pytest.raises(ValueError, match="box template 'test-template'"):
            await _inventory(core_v1).check_templates()
    else:
        await _inventory(core_v1).check_templates()


async def test_creating_an_existing_box_returns_it_without_stamping_another() -> None:
    core_v1 = FakeCoreV1(pods={OBJECT_NAME: _pod(ready=True)})
    info = await _inventory(core_v1).create(CALLER, "box", "test-template")
    assert core_v1.created == []
    assert info.phase == "Running"


async def test_a_refused_create_says_what_the_api_server_said() -> None:
    """A full namespace quota refuses the Pod outright, and the caller can act on that by disposing
    boxes it has finished with."""
    refusal = ApiException(status=403, reason="Forbidden")
    refusal.body = json.dumps({"message": 'pods "caller-one-box" is forbidden: exceeded quota: test-quota'}).encode()
    with pytest.raises(SandboxActionError, match="exceeded quota: test-quota"):
        await _inventory(FakeCoreV1(refuse_create=refusal)).create(CALLER, "box", "test-template")


async def test_another_callers_box_of_that_name_is_not_reached() -> None:
    core_v1 = FakeCoreV1(pods={OBJECT_NAME: _pod(ready=True, caller="caller-two")})
    with pytest.raises(ForeignSandboxError):
        await _inventory(core_v1).get(CALLER, "box")


async def test_every_condition_reaches_the_caller_as_kubernetes_wrote_it() -> None:
    """A Pod reports more than readiness, and summarising it here would drop the rest: an
    unschedulable box and one still pulling its image are both "not Ready"."""
    pod = _pod(ready=False)
    pod.status.conditions.insert(
        0,
        V1PodCondition(
            type="PodScheduled",
            status="False",
            reason="Unschedulable",
            message="0/3 nodes are available",
            last_transition_time=datetime(2026, 9, 19, 9, 49, 30, tzinfo=UTC),
        ),
    )
    info = await _inventory(FakeCoreV1(pods={OBJECT_NAME: pod})).get(CALLER, "box")
    scheduled, ready = info.conditions
    assert (scheduled.type, scheduled.status, scheduled.reason) == ("PodScheduled", "False", "Unschedulable")
    assert scheduled.last_transition_time is not None
    assert (ready.type, ready.status, ready.reason) == (READY_CONDITION, "False", "ContainersNotReady")


async def test_a_box_with_no_status_yet_reports_no_conditions() -> None:
    """Kubernetes has not written one between the create call and the read that follows it."""
    pod = _pod(ready=False)
    pod.status = V1PodStatus()
    info = await _inventory(FakeCoreV1(pods={OBJECT_NAME: pod})).get(CALLER, "box")
    assert (info.phase, info.conditions) == (None, [])


@pytest.mark.parametrize(
    ("annotations", "container"),
    [(None, "workspace"), ({DEFAULT_CONTAINER_ANNOTATION: "egress-sidecar"}, "egress-sidecar")],
)
async def test_exec_runs_in_the_container_kubectl_exec_would_pick(
    annotations: dict[str, str] | None, container: str
) -> None:
    """The box's Pod names its default container, else its first is the one."""
    runner = FakeExecRunner()
    inventory = _inventory(FakeCoreV1(pods={OBJECT_NAME: _pod(ready=True, annotations=annotations)}), runner)
    await inventory.execute(CALLER, "box", script="true", cwd=None, timeout_seconds=5, max_output_bytes=100)
    assert (runner.ran_against, runner.ran_in) == ([OBJECT_NAME], [container])


async def test_exec_in_a_box_that_is_not_ready_says_why() -> None:
    runner = FakeExecRunner()
    inventory = _inventory(
        FakeCoreV1(pods={OBJECT_NAME: _pod(ready=False, message="containers with unready status: [workspace]")}), runner
    )
    with pytest.raises(SandboxActionError, match=r"not ready \(containers with unready status"):
        await inventory.execute(CALLER, "box", script="true", cwd=None, timeout_seconds=5, max_output_bytes=100)
    assert runner.ran_against == []


async def test_an_offered_template_is_shown_as_kubernetes_json() -> None:
    async with ApiClient() as api:
        shown = await _inventory(FakeCoreV1(), api=api).get_template("test-template")
    assert shown == {
        "metadata": {"labels": {"app.kubernetes.io/name": "test-box"}},
        "spec": {
            "automountServiceAccountToken": False,
            "containers": [{"name": "workspace"}, {"name": "egress-sidecar"}],
        },
    }


async def test_a_template_this_deployment_does_not_offer_is_refused() -> None:
    inventory = _inventory(FakeCoreV1())
    with pytest.raises(SandboxActionError, match="unknown template 'another-template'; this deployment offers"):
        await inventory.create(CALLER, "box", "another-template")
    with pytest.raises(SandboxActionError, match="unknown template 'another-template'; this deployment offers"):
        await inventory.get_template("another-template")


if __name__ == "__main__":
    pytest_bazel.main()
