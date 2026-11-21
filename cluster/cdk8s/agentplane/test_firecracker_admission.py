"""Admission for staging's controller-owned launcher Pods and command exec."""

import json
import os
import subprocess
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

import pytest
import pytest_bazel
from cdk8s import App, Chart, Testing as Cdk8sTesting
from kubernetes_asyncio import client as k8s_client
from python.runfiles import Runfiles

from agentplane.subjects import ServiceAccountRef
from agentplane.testing.kubernetes_apiserver import dry_run_admitted, kubernetes_apiserver, wait_until_refused
from cluster.cdk8s.agentplane import environment_firecracker, staging
from cluster.cdk8s.agentplane.firecracker_admission import add_firecracker_admission
from cluster.cdk8s.testing.manifests import find_resource

NAMESPACE = "test-firecracker"
CONTROLLER = f"system:serviceaccount:{NAMESPACE}:environments"
OTHER = f"system:serviceaccount:{NAMESPACE}:other"
ACTIONS = f"system:serviceaccount:{NAMESPACE}:actions"
_PREFIX = "agentplane-staging-"


def _launcher(app: App) -> dict[str, Any]:
    chart = staging.chart(app, replace(staging.ENV, firecracker_sandboxes=True))
    manifests = cast(list[dict[str, Any]], Cdk8sTesting.synth(chart))
    template = find_resource(manifests, "SandboxTemplate", environment_firecracker.NAME)
    # Real staging resolves the same repositories to these tag-shaped immutable build references.
    firecracker = template["spec"]["firecracker"]
    tag = ":devel-20261120204116-3a4cafa"
    for key in ("bootImage", "runnerImage"):
        firecracker[key] = firecracker[key].removesuffix(":unset") + tag
    for container in (*firecracker["pod"]["spec"]["containers"], *firecracker["pod"]["spec"]["initContainers"]):
        container["image"] = container["image"].removesuffix(":unset") + tag
    binary = Runfiles.CreateOrRaise().Rlocation(os.environ["RENDER_FIRECRACKER_POD_RLOCATION"])
    completed = subprocess.run(
        [str(binary), "--namespace", NAMESPACE], input=json.dumps(template), text=True, capture_output=True, check=True
    )
    return cast(dict[str, Any], json.loads(completed.stdout))


async def test_only_the_controller_can_launch_the_firecracker_shape(scratch_chart: Chart, scratch_app: App) -> None:
    add_firecracker_admission(
        scratch_chart,
        ServiceAccountRef(namespace=NAMESPACE, name="environments"),
        actions=ServiceAccountRef(namespace=NAMESPACE, name="actions"),
        command_container="sandbox",
    )
    manifests = cast(list[dict[str, Any]], Cdk8sTesting.synth(scratch_chart))
    async with kubernetes_apiserver() as server:
        launcher = _PREFIX + "firecracker-launcher"
        await server.install_admission_policy(
            find_resource(manifests, "ValidatingAdmissionPolicy", launcher),
            find_resource(manifests, "ValidatingAdmissionPolicyBinding", launcher),
            namespaces=[NAMESPACE],
        )
        core = k8s_client.CoreV1Api(server.api)
        await core.create_namespaced_service_account(
            NAMESPACE, k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name="default"))
        )
        admission = k8s_client.AdmissionregistrationV1Api(server.api)
        for name in (_PREFIX + "firecracker-config", _PREFIX + "actions-command-exec"):
            await admission.create_validating_admission_policy(
                cast(Any, find_resource(manifests, "ValidatingAdmissionPolicy", name))
            )
            await admission.create_validating_admission_policy_binding(
                cast(Any, find_resource(manifests, "ValidatingAdmissionPolicyBinding", name))
            )

        async def admitted(user: str, pod: dict[str, Any], namespace: str = NAMESPACE) -> bool:
            async with server.impersonating(user) as client:
                return await dry_run_admitted(
                    k8s_client.CoreV1Api(client).create_namespaced_pod(namespace, cast(Any, pod), dry_run="All")
                )

        valid = _launcher(scratch_app)
        assert valid["spec"]["serviceAccountName"] == "default"
        assert valid["spec"]["automountServiceAccountToken"] is False
        svid = next(v for v in valid["spec"]["volumes"] if v["name"] == "agentplane-environment-runtime-tls")
        assert any("secret" in source for source in svid["projected"]["sources"])
        await wait_until_refused(lambda: admitted(OTHER, valid))
        async with server.impersonating(CONTROLLER) as client:
            await k8s_client.CoreV1Api(client).create_namespaced_pod(NAMESPACE, cast(Any, valid), dry_run="All")

        unowned = deepcopy(valid)
        del unowned["metadata"]["ownerReferences"]
        assert not await admitted(CONTROLLER, unowned)
        for spec in ({"hostNetwork": True}, {"hostPID": True}, {"automountServiceAccountToken": True}):
            assert not await admitted(CONTROLLER, {**valid, "spec": {**valid["spec"], **spec}})
        wrong_volume = deepcopy(valid)
        tun = next(v for v in wrong_volume["spec"]["volumes"] if v["name"] == "tun")
        tun["hostPath"]["path"] = "/dev/kvm"
        assert not await admitted(CONTROLLER, wrong_volume)
        wrong_image = deepcopy(valid)
        wrong_image["spec"]["containers"][0]["image"] = "registry.test/wrong-launcher:devel-20261120204116-3a4cafa"
        assert not await admitted(CONTROLLER, wrong_image)
        command_override = deepcopy(valid)
        command_override["spec"]["containers"][0]["command"] = ["/bin/bash"]
        assert not await admitted(CONTROLLER, command_override)
        unreviewed_env = deepcopy(valid)
        unreviewed_env["spec"]["containers"][0]["env"] = [{"name": "PATH", "value": "/custom"}]
        assert not await admitted(CONTROLLER, unreviewed_env)
        changed_config = deepcopy(valid)
        settings = next(v for v in changed_config["spec"]["volumes"] if v["name"] == "firecracker-settings")
        settings["configMap"]["name"] = "unreviewed"
        assert not await admitted(CONTROLLER, changed_config)
        wrong_svid = deepcopy(valid)
        svid_volume = next(
            v for v in wrong_svid["spec"]["volumes"] if v["name"] == "agentplane-environment-runtime-tls"
        )
        secret = next(p["secret"] for p in svid_volume["projected"]["sources"] if "secret" in p)
        secret["name"] = "svid-another-sandbox-0123456789abcdef0123456789abcdef"
        assert not await admitted(CONTROLLER, wrong_svid)
        changed_relay = deepcopy(valid)
        changed_relay["spec"]["initContainers"][0]["image"] = "registry.test/another-relay"
        assert not await admitted(CONTROLLER, changed_relay)
        privileged_sidecar = deepcopy(valid)
        privileged_sidecar["spec"]["initContainers"][0]["securityContext"]["privileged"] = True
        assert not await admitted(CONTROLLER, privileged_sidecar)

        regular = {
            "metadata": {"name": "ordinary"},
            "spec": {"containers": [{"name": "app", "image": "registry.test/app"}]},
        }
        assert await admitted(OTHER, regular)

        async def config_admitted(user: str, name: str) -> bool:
            async with server.impersonating(user) as client:
                return await dry_run_admitted(
                    k8s_client.CoreV1Api(client).create_namespaced_config_map(
                        NAMESPACE, k8s_client.V1ConfigMap(metadata=k8s_client.V1ObjectMeta(name=name)), dry_run="All"
                    )
                )

        await wait_until_refused(lambda: config_admitted(CONTROLLER, "environment-firecracker-launcher"))
        assert await config_admitted(CONTROLLER, "test-sandbox-launch")
        assert await config_admitted(OTHER, "environment-firecracker-launcher")

        async with server.impersonating(ACTIONS) as client:
            action = k8s_client.CoreV1Api(client)

            async def reaches_exec(container: str | None) -> bool:
                # No Pods are scheduled in this fixture. A missing Pod error proves the request
                # reached the exec handler after admission and no workload command ran.
                if container is None:
                    request = action.connect_get_namespaced_pod_exec("does-not-exist", NAMESPACE, command="true")
                else:
                    request = action.connect_get_namespaced_pod_exec(
                        "does-not-exist", NAMESPACE, command="true", container=container
                    )
                with pytest.raises(k8s_client.ApiException) as raised:
                    await request
                if raised.value.status == 404:
                    return True
                assert "actions-command-exec" in str(raised.value)
                return False

            await wait_until_refused(lambda: reaches_exec("environment-runtime"))
            assert not await reaches_exec(None)
            assert await reaches_exec("sandbox")


if __name__ == "__main__":
    pytest_bazel.main()
