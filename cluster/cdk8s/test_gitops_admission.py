"""Check the native audit matcher with real admission requests and impersonated identities."""

from __future__ import annotations

from typing import Any, cast

import cdk8s
import pytest_bazel
from cdk8s import Chart
from kubernetes_asyncio import client as k8s_client
from more_itertools import one

from agentplane.testing.kubernetes_apiserver import dry_run_admitted, kubernetes_apiserver, wait_until_refused
from cluster.cdk8s.gitops_admission import add_gitops_write_audit


def _deployment(namespace: str, name: str) -> k8s_client.V1Deployment:
    labels = {"app": name}
    return k8s_client.V1Deployment(
        metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
        spec=k8s_client.V1DeploymentSpec(
            selector=k8s_client.V1LabelSelector(match_labels=labels),
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(labels=labels),
                spec=k8s_client.V1PodSpec(
                    containers=[k8s_client.V1Container(name="probe", image="registry.invalid/unused")]
                ),
            ),
        ),
    )


async def test_gitops_audit_matches_direct_writes(scratch_chart: Chart) -> None:
    add_gitops_write_audit(scratch_chart)
    documents = cast(list[dict[str, Any]], cdk8s.Testing.synth(scratch_chart))
    policy = one(doc for doc in documents if doc["kind"] == "ValidatingAdmissionPolicy")
    binding = one(doc for doc in documents if doc["kind"] == "ValidatingAdmissionPolicyBinding")
    assert binding["spec"]["validationActions"] == ["Audit"]
    assert policy["spec"]["failurePolicy"] == "Ignore"
    rule = one(policy["spec"]["matchConstraints"]["resourceRules"])
    assert set(rule["resources"]) == {"deployments", "statefulsets", "daemonsets"}
    assert set(rule["operations"]) == {"CREATE", "UPDATE"}
    helm = "system:serviceaccount:flux-system:helm-controller"
    crossplane = "system:serviceaccount:crossplane-system:crossplane"

    async with kubernetes_apiserver() as server:
        await server.install_admission_policy(
            policy, binding, namespaces=["authentik", "crossplane-system", "flux-system"]
        )
        apps = k8s_client.AppsV1Api(server.api)
        await apps.create_namespaced_deployment("authentik", _deployment("authentik", "existing"))

        async def create_admitted(username: str, namespace: str) -> bool:
            async with server.impersonating(username) as client:
                return await dry_run_admitted(
                    k8s_client.AppsV1Api(client).create_namespaced_deployment(
                        namespace, _deployment(namespace, "new"), dry_run="All"
                    )
                )

        assert await create_admitted("direct-writer", "authentik")
        # A test-only Deny binding makes the same matcher observable in envtest,
        # whose API server does not run our production audit log configuration.
        test_binding = {
            **binding,
            "metadata": {"name": "test-gitops-matcher"},
            "spec": {**binding["spec"], "validationActions": ["Deny"]},
        }
        admission = k8s_client.AdmissionregistrationV1Api(server.api)
        await admission.create_validating_admission_policy_binding(cast(Any, test_binding))
        await wait_until_refused(lambda: create_admitted("direct-writer", "authentik"))

        assert await create_admitted(helm, "authentik")
        assert await create_admitted("system:serviceaccount:flux-system:kustomize-controller", "authentik")
        assert await create_admitted("system:serviceaccount:flux-system:haku-state-reconciler", "authentik")
        assert await create_admitted(crossplane, "crossplane-system")
        assert not await create_admitted(crossplane, "authentik")
        assert await create_admitted("direct-writer", "kube-system")
        assert await create_admitted("direct-writer", "flux-system")
        assert not await create_admitted("system:serviceaccount:authentik:helm-controller", "authentik")

        update = await apps.read_namespaced_deployment("existing", "authentik")
        update.metadata.labels = {"changed": "true"}
        assert not await dry_run_admitted(
            apps.replace_namespaced_deployment("existing", "authentik", update, dry_run="All")
        )
        async with server.impersonating(helm) as client:
            assert await dry_run_admitted(
                k8s_client.AppsV1Api(client).replace_namespaced_deployment(
                    "existing", "authentik", update, dry_run="All"
                )
            )
        await apps.delete_namespaced_deployment("existing", "authentik", dry_run="All")


if __name__ == "__main__":
    pytest_bazel.main()
