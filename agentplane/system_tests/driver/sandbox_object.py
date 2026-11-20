"""Create a Sandbox object directly through the Kubernetes API."""

import json
from typing import Any

from agentplane.sandbox_resource import SANDBOX_API_VERSION, SANDBOX_KIND
from agentplane.system_tests.driver.cluster import Cluster


async def create_sandbox_object(
    cluster: Cluster, name: str, *, template: str, identity: str, running: bool, **extra_spec: object
) -> dict[str, Any]:
    """Create the Sandbox `name` acting as Identity `identity` and return the created object.

    `extra_spec` adds optional spec fields such as `wakeOnSend`.
    """
    created: dict[str, Any] = json.loads(
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            "-o",
            "json",
            body={
                "apiVersion": SANDBOX_API_VERSION,
                "kind": SANDBOX_KIND,
                "metadata": {"name": name, "namespace": cluster.namespace},
                "spec": {"template": template, "running": running, "identity": {"name": identity}, **extra_spec},
            },
        )
    )
    return created
