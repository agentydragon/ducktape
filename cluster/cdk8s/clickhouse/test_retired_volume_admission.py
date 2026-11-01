"""The retired PVC guard checks the actual object UID on Kubernetes DELETE."""

from typing import Any

import pytest_bazel
import yaml
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver
from util.bazel.runfiles import get_required_path, own_repo_rlocation


async def test_retired_claim_uid_guard() -> None:
    path = get_required_path(own_repo_rlocation("cluster/k8s/clickhouse/cluster/retired-volume-reclaim.yaml"))
    policy, binding, *_ = list(yaml.safe_load_all(path.read_text()))
    name = "data-piraeus-hdd-chi-clickhouse-default-0-0-0"
    namespace = "clickhouse"
    expected_uid = "05bb6184-88de-4de2-a74c-abe6c9da5663"
    assert expected_uid in policy["spec"]["validations"][0]["expression"]

    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        for ns in (namespace, "other"):
            await core.create_namespace(
                k8s_client.V1Namespace(
                    metadata=k8s_client.V1ObjectMeta(name=ns, labels={"kubernetes.io/metadata.name": ns})
                )
            )

        async def create_claim(ns: str, claim_name: str) -> Any:
            return await core.create_namespaced_persistent_volume_claim(
                ns,
                k8s_client.V1PersistentVolumeClaim(
                    metadata=k8s_client.V1ObjectMeta(name=claim_name),
                    spec=k8s_client.V1PersistentVolumeClaimSpec(
                        access_modes=["ReadWriteOnce"],
                        resources=k8s_client.V1VolumeResourceRequirements(requests={"storage": "1Gi"}),
                    ),
                ),
            )

        guarded = await create_claim(namespace, name)
        await create_claim(namespace, "other-claim")
        await create_claim("other", name)
        assert guarded.metadata.uid != expected_uid

        admission = k8s_client.AdmissionregistrationV1Api(server.api)
        await admission.create_validating_admission_policy(policy)
        await admission.create_validating_admission_policy_binding(binding)

        async def allowed(ns: str, claim_name: str) -> bool:
            try:
                await core.delete_namespaced_persistent_volume_claim(claim_name, ns, dry_run="All")
            except ApiException as exc:
                if exc.status not in (403, 422):
                    raise
                return False
            return True

        # The API server loads admission policies asynchronously.
        async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                assert not await allowed(namespace, name)
        assert await allowed(namespace, "other-claim")
        assert await allowed("other", name)

        # Reuse the same guard with the actual test object's UID to exercise its allow path.
        current = await admission.read_validating_admission_policy(policy["metadata"]["name"])
        policy["metadata"]["resourceVersion"] = current.metadata.resource_version
        policy["spec"]["validations"][0]["expression"] = f"oldObject.metadata.uid == '{guarded.metadata.uid}'"
        await admission.replace_validating_admission_policy(policy["metadata"]["name"], policy)
        async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
            with attempt:
                assert await allowed(namespace, name)


if __name__ == "__main__":
    pytest_bazel.main()
