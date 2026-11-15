"""Release one acknowledged Sandbox incarnation before the fixture stack stops."""

import json

from kubernetes_asyncio import client as k8s

from agentplane.crd_group import GROUP, VERSION
from agentplane.system_tests.driver.cluster import Cluster
from agentplane.system_tests.driver.tracing import tracer


async def delete_sandbox(cluster: Cluster, name: str, uid: str) -> None:
    with tracer.start_as_current_span(
        "system_test.central_sampler.sandbox_cleanup", record_exception=False, set_status_on_exception=False
    ):
        await cluster.kubectl(
            "patch",
            "sandbox",
            name,
            "--type=merge",
            "-p",
            json.dumps({"metadata": {"uid": uid}, "spec": {"running": False}}),
        )
        await cluster.kubectl(
            "wait", f"sandbox/{name}", "--for=jsonpath={.status.operatingMode}=Suspended", "--timeout=300s"
        )
        await cluster.kubectl("wait", f"sandbox/{name}", "--for=jsonpath={.status.stopStage}=Stopped", "--timeout=300s")
        async with cluster.api_client() as connection:
            await k8s.CustomObjectsApi(connection).delete_namespaced_custom_object(
                GROUP,
                VERSION,
                cluster.namespace,
                "sandboxes",
                name,
                body=k8s.V1DeleteOptions(preconditions=k8s.V1Preconditions(uid=uid)),
            )
        await cluster.kubectl("wait", f"sandbox/{name}", "--for=delete", "--timeout=120s")
