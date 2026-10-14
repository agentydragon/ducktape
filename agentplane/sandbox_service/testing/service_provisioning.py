"""Sandbox Service provisioning around the shared Kubernetes test cluster."""

from typing import cast

from kubernetes_asyncio import client as k8s_client

from agentplane.sandbox_service.egress import EgressInventory
from agentplane.sandbox_service.provisioning import Provisioning
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from util.kubernetes import CustomObjectsClient


def service_provisioning(cluster: SandboxCluster) -> Provisioning:
    custom_objects = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(cluster.api))
    return Provisioning(
        cluster.inventory,
        EgressInventory(namespace=cluster.inventory.namespace, custom_objects=custom_objects),
        default_policy_sets=frozenset(),
    )
