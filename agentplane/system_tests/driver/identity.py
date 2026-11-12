"""An Identity for a scenario that writes its Sandbox object directly, granted as a launch grants it."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.crd_group import GROUP
from agentplane.system_tests.driver.cluster import Cluster

API_VERSION = f"{GROUP}/v1alpha1"


# The baseline sets every preset grants, which these preset-less launches name themselves: the
# deployment fixture's DEFAULT_POLICIES (cluster/cdk8s/agentplane/egress.py). Kept literal so the
# driver does not import cdk8s.
LAUNCH_DEFAULT_POLICY_SETS = ("basic", "inference-experiments")


@asynccontextmanager
async def scenario_identity(cluster: Cluster, name: str, policy_sets: list[str]) -> AsyncIterator[str]:
    """Create the Identity of this name and one binding of policy_sets that it owns, as a launch
    does (PolicyService GrantIdentityPolicyBinding); yield its name for a Sandbox's spec.identity;
    delete it, and by garbage collection its binding, once the scenario is done with it."""
    identity = json.loads(
        await cluster.kubectl(
            "create",
            "-f",
            "-",
            "-o",
            "json",
            body={
                "apiVersion": API_VERSION,
                "kind": "Identity",
                "metadata": {"name": name, "namespace": cluster.namespace},
                "spec": {"description": f"System-test agent of Sandbox {name}."},
            },
        )
    )
    try:
        if policy_sets:
            await cluster.kubectl(
                "create",
                "-f",
                "-",
                body={
                    "apiVersion": API_VERSION,
                    "kind": "PolicyBinding",
                    "metadata": {
                        "name": f"{name}.identity",
                        "namespace": cluster.namespace,
                        "ownerReferences": [
                            {
                                "apiVersion": API_VERSION,
                                "kind": "Identity",
                                "name": name,
                                "uid": identity["metadata"]["uid"],
                            }
                        ],
                    },
                    "spec": {"subject": {"identity": {"name": name}}, "policySets": sorted(policy_sets)},
                },
            )
        yield name
    finally:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(RuntimeError), stop=stop_after_delay(60), wait=wait_fixed(2), reraise=True
        ):
            with attempt:
                await cluster.kubectl("delete", "identity", name, "--ignore-not-found", "--wait=false")
