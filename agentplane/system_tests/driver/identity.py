"""An Identity for a scenario that writes its Sandbox object directly, granted as a launch grants it."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TypedDict

from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.exceptions import ApiException

from agentplane.crd_group import GROUP
from agentplane.system_tests.driver.cluster import Cluster

API_VERSION = f"{GROUP}/v1alpha1"


# These preset-less launches grant the Identity-compatible baseline. Standing ServiceAccount
# callers retain the projected-token route in `basic`. Kept literal so the driver does not import cdk8s.
LAUNCH_DEFAULT_POLICY_SETS = ("basic-identity", "inference-experiments")


class ScenarioIdentity(TypedDict):
    namespace: str
    name: str
    uid: str


class ScenarioIdentityReferencedError(RuntimeError):
    """A fixture Identity still has a Sandbox, grant, or workload credential reference."""


async def create_scenario_identity(cluster: Cluster, name: str, policy_sets: list[str]) -> ScenarioIdentity:
    """Create one fixture Identity and its Identity-owned PolicySet grant."""
    created = json.loads(
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
    metadata = created["metadata"]
    identity: ScenarioIdentity = {"namespace": metadata["namespace"], "name": metadata["name"], "uid": metadata["uid"]}
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
                        {"apiVersion": API_VERSION, "kind": "Identity", "name": name, "uid": identity["uid"]}
                    ],
                },
                "spec": {"subject": {"identity": {"name": name}}, "policySets": sorted(policy_sets)},
            },
        )
    return identity


async def delete_scenario_identity(cluster: Cluster, identity: ScenarioIdentity) -> None:
    """Delete only an unreferenced fixture Identity incarnation that was created."""
    namespace = f"--namespace={identity['namespace']}"
    current_text = await cluster.kubectl(
        "get", "identity", identity["name"], namespace, "--ignore-not-found", "-o", "json"
    )
    if not current_text:
        return
    current = json.loads(current_text)
    if current.get("metadata", {}).get("uid") != identity["uid"]:
        return

    sandboxes = json.loads(await cluster.kubectl("get", "sandboxes", namespace, "-o", "json"))["items"]
    for sandbox in sandboxes:
        if sandbox.get("spec", {}).get("identity", {}).get("name") == identity["name"]:
            raise ScenarioIdentityReferencedError(
                f"Sandbox/{sandbox['metadata']['namespace']}/{sandbox['metadata']['name']} references Identity"
            )

    async with cluster.api_client() as api:
        try:
            await k8s.CustomObjectsApi(api).delete_namespaced_custom_object(
                group=GROUP,
                version="v1alpha1",
                namespace=identity["namespace"],
                plural="identities",
                name=identity["name"],
                body=k8s.V1DeleteOptions(
                    preconditions=k8s.V1Preconditions(uid=identity["uid"]), propagation_policy="Background"
                ),
            )
        except ApiException as error:
            if error.status == 404:
                return
            raise


@asynccontextmanager
async def scenario_identity(cluster: Cluster, name: str, policy_sets: list[str]) -> AsyncIterator[str]:
    """Create the Identity of this name and one binding of policy_sets that it owns, as a launch
    does (PolicyService GrantIdentityPolicyBinding); yield its name for a Sandbox's spec.identity;
    delete it, and by garbage collection its binding, once the scenario is done with it."""
    identity = await create_scenario_identity(cluster, name, policy_sets)
    try:
        yield name
    finally:
        await delete_scenario_identity(cluster, identity)
