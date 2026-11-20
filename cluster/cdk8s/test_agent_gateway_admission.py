"""Exercise the generated sandbox Gateway API rule against a real API server."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, cast

import cdk8s
import pytest_bazel
import yaml
from cdk8s import Chart
from kubernetes_asyncio import client as k8s_client
from more_itertools import one
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.testing.kubernetes_apiserver import dry_run_admitted, kubernetes_apiserver, wait_until_refused
from cluster.cdk8s.gateway import add_agent_gateway_admission

GROUP = "gateway.networking.k8s.io"
RESOURCE = "httproutes"
SANDBOXES = ("claude-sandbox", "haku-sandbox")
OPERATOR_NAMESPACE = "authentik"


def _route(namespace: str, version: str, name: str) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{version}",
        "kind": "HTTPRoute",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"rules": [{"backendRefs": [{"name": "test-service", "port": 80}]}]},
    }


async def test_agent_route_admission(scratch_chart: Chart) -> None:
    add_agent_gateway_admission(scratch_chart)
    documents = cast(list[dict[str, Any]], cdk8s.Testing.synth(scratch_chart))
    policy = one(resource for resource in documents if resource["kind"] == "ValidatingAdmissionPolicy")
    binding = one(resource for resource in documents if resource["kind"] == "ValidatingAdmissionPolicyBinding")
    runfiles = Runfiles.CreateOrRaise()
    crd_path = Path(str(runfiles.Rlocation(os.environ["GATEWAY_API_HTTPROUTE_CRD"])))
    crd = yaml.safe_load(await asyncio.to_thread(crd_path.read_text))

    async with kubernetes_apiserver() as server:
        extensions = k8s_client.ApiextensionsV1Api(server.api)
        await extensions.create_custom_resource_definition(crd)
        async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                installed = await extensions.read_custom_resource_definition(crd["metadata"]["name"])
                assert any(c.type == "Established" and c.status == "True" for c in installed.status.conditions or [])

        core = k8s_client.CoreV1Api(server.api)
        objects = k8s_client.CustomObjectsApi(server.api)
        for namespace in (*SANDBOXES, OPERATOR_NAMESPACE):
            await core.create_namespace(k8s_client.V1Namespace(metadata=k8s_client.V1ObjectMeta(name=namespace)))
            # Seed a route before binding the policy so UPDATE and DELETE can be tested separately.
            await objects.create_namespaced_custom_object(
                GROUP, "v1", namespace, RESOURCE, _route(namespace, "v1", "existing")
            )

        await server.install_admission_policy(policy, binding, namespaces=[])

        async def create_admitted(namespace: str, version: str) -> bool:
            return await dry_run_admitted(
                objects.create_namespaced_custom_object(
                    GROUP, version, namespace, RESOURCE, _route(namespace, version, "new"), dry_run="All"
                )
            )

        await wait_until_refused(lambda: create_admitted(SANDBOXES[0], "v1"))
        for version in ("v1", "v1beta1"):
            for namespace in SANDBOXES:
                assert not await create_admitted(namespace, version)
            assert await create_admitted(OPERATOR_NAMESPACE, version)

        for namespace in (*SANDBOXES, OPERATOR_NAMESPACE):
            replacement = await objects.get_namespaced_custom_object(GROUP, "v1", namespace, RESOURCE, "existing")
            replacement["metadata"]["labels"] = {"test": "updated"}
            admitted = await dry_run_admitted(
                objects.replace_namespaced_custom_object(
                    GROUP, "v1", namespace, RESOURCE, "existing", replacement, dry_run="All"
                )
            )
            assert admitted == (namespace == OPERATOR_NAMESPACE)
            await objects.patch_namespaced_custom_object_status(
                GROUP,
                "v1",
                namespace,
                RESOURCE,
                "existing",
                [{"op": "add", "path": "/status", "value": {"parents": []}}],
                dry_run="All",
            )
            await objects.delete_namespaced_custom_object(GROUP, "v1", namespace, RESOURCE, "existing", dry_run="All")

        # The admission rule covers only the six Gateway API kinds, even inside the sandboxes.
        for namespace in SANDBOXES:
            await core.create_namespaced_config_map(
                namespace, k8s_client.V1ConfigMap(metadata=k8s_client.V1ObjectMeta(name="unrelated")), dry_run="All"
            )


if __name__ == "__main__":
    pytest_bazel.main()
