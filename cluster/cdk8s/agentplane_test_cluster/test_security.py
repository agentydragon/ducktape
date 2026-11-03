"""The inner cluster's namespace fence applies at admission, even after broad authorization."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import cdk8s
import pytest_bazel
from cdk8s import App
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config import new_client_from_config
from tenacity import AsyncRetrying, stop_after_delay, wait_fixed

from agentplane.testing.kubernetes_apiserver import kubernetes_apiserver
from cluster.cdk8s.agentplane_test_cluster import security

_OTHER_NAMESPACE = "system-test-outside"
_SERVICE_ACCOUNT = "test-broadly-authorized"
_CLUSTER_ROLE = "inner-security-test-role"
_CLUSTER_ROLE_BINDING = "inner-security-test-binding"
_DENIAL_MESSAGE = "Tenant ServiceAccounts may only write namespaced resources in their tenant namespaces."

Body = dict[str, Any]


def _objects(app: App) -> list[Body]:
    return cdk8s.Testing.synth(security.chart(app))


def _one(objects: list[Body], kind: str) -> Body:
    [resource] = (
        obj
        for obj in objects
        if obj["kind"] == kind and obj["metadata"]["name"] == f"{security.NAME}-tenant-namespace-fence"
    )
    return resource


def test_inner_chart_has_only_tenant_namespaces_and_clusterwide_fail_closed_policy(scratch_app: App) -> None:
    objects = _objects(scratch_app)
    namespaces = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "Namespace"}
    policy = _one(objects, "ValidatingAdmissionPolicy")
    binding = _one(objects, "ValidatingAdmissionPolicyBinding")

    assert set(namespaces) == set(security.TENANT_NAMESPACES)
    for namespace in namespaces.values():
        labels = namespace["metadata"]["labels"]
        assert labels["app.kubernetes.io/managed-by"] == "agentplane-test-cluster"
        for level in ("enforce", "audit", "warn"):
            assert labels[f"pod-security.kubernetes.io/{level}"] == "baseline"
            assert labels[f"pod-security.kubernetes.io/{level}-version"] == "v1.34"

    spec = policy["spec"]
    assert spec["failurePolicy"] == "Fail"
    rule = spec["matchConstraints"]["resourceRules"][0]
    assert set(rule["operations"]) == {"CREATE", "UPDATE", "DELETE", "CONNECT"}
    assert rule["resources"] == ["*/*"]
    assert rule["scope"] == "*"
    assert spec["matchConstraints"]["excludeResourceRules"] == [
        {
            "apiGroups": ["authentication.k8s.io"],
            "apiVersions": ["v1"],
            "operations": ["CREATE"],
            "resources": ["tokenreviews", "selfsubjectreviews"],
            "scope": "*",
        },
        {
            "apiGroups": ["authorization.k8s.io"],
            "apiVersions": ["v1"],
            "operations": ["CREATE"],
            "resources": [
                "subjectaccessreviews",
                "localsubjectaccessreviews",
                "selfsubjectaccessreviews",
                "selfsubjectrulesreviews",
            ],
            "scope": "*",
        },
    ]
    assert all(
        f"system:serviceaccount:{namespace}:" in spec["matchConditions"][0]["expression"]
        for namespace in security.TENANT_NAMESPACES
    )
    expression = spec["validations"][0]["expression"]
    assert "request.namespace in" in expression
    assert "request.resource.resource == 'namespaces'" in expression
    assert spec["validations"][0]["message"] == _DENIAL_MESSAGE
    binding_spec = binding["spec"]
    assert binding_spec["policyName"] == policy["metadata"]["name"]
    assert binding_spec["validationActions"] == ["Deny"]
    # A binding namespace selector would skip cluster-scoped writes before CEL can reject them.
    assert "matchResources" not in binding_spec
    assert {obj["kind"] for obj in objects} == {
        "Namespace",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }


def _cluster_role_binding(name: str) -> k8s_client.V1ClusterRoleBinding:
    return k8s_client.V1ClusterRoleBinding(
        metadata=k8s_client.V1ObjectMeta(name=name),
        role_ref=k8s_client.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name=_CLUSTER_ROLE),
        subjects=[
            k8s_client.RbacV1Subject(
                kind="ServiceAccount", name=_SERVICE_ACCOUNT, namespace=security.TENANT_NAMESPACES[0]
            )
        ],
    )


async def _is_rejected(operation: Awaitable[object], message: str) -> bool:
    error: ApiException | None = None
    try:
        await operation
    except ApiException as caught:
        error = caught
    if error is None:
        return False
    assert error.status in (403, 422), error.body
    assert message in str(error.body), error.body
    return True


async def _is_policy_denial(operation: Awaitable[object]) -> bool:
    return await _is_rejected(operation, _DENIAL_MESSAGE)


async def _wait_for_policy(operation: Callable[[], Awaitable[object]]) -> None:
    async for attempt in AsyncRetrying(stop=stop_after_delay(30), wait=wait_fixed(0.1), reraise=True):
        with attempt:
            assert await _is_policy_denial(operation())


async def test_namespace_fence_blocks_tested_writes_even_with_system_masters(
    scratch_app_factory: Callable[[], App],
) -> None:
    objects = _objects(scratch_app_factory())
    policy = _one(objects, "ValidatingAdmissionPolicy")
    binding = _one(objects, "ValidatingAdmissionPolicyBinding")

    async with kubernetes_apiserver() as server:
        core = k8s_client.CoreV1Api(server.api)
        admin_rbac = k8s_client.RbacAuthorizationV1Api(server.api)
        for namespace in (obj for obj in objects if obj["kind"] == "Namespace"):
            await core.create_namespace(cast(Any, namespace))
        await core.create_namespace(
            k8s_client.V1Namespace(
                metadata=k8s_client.V1ObjectMeta(
                    name=_OTHER_NAMESPACE,
                    labels={
                        "pod-security.kubernetes.io/enforce": "baseline",
                        "pod-security.kubernetes.io/enforce-version": "v1.34",
                    },
                )
            )
        )
        await core.create_namespaced_service_account(
            security.TENANT_NAMESPACES[0],
            k8s_client.V1ServiceAccount(metadata=k8s_client.V1ObjectMeta(name=_SERVICE_ACCOUNT)),
        )

        # PSA baseline applies even to an API administrator; dry-run admission does not create Pods.
        safe_pod = k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(name="baseline-safe-pod"),
            spec=k8s_client.V1PodSpec(
                containers=[k8s_client.V1Container(name="fixture", image="example.invalid/unused:test")]
            ),
        )
        await core.create_namespaced_pod(security.TENANT_NAMESPACES[0], safe_pod, dry_run="All")
        privileged_pod = k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(name="baseline-privileged-pod"),
            spec=k8s_client.V1PodSpec(
                containers=[
                    k8s_client.V1Container(
                        name="fixture",
                        image="example.invalid/unused:test",
                        security_context=k8s_client.V1SecurityContext(privileged=True),
                    )
                ]
            ),
        )
        assert await _is_rejected(
            core.create_namespaced_pod(security.TENANT_NAMESPACES[0], privileged_pod, dry_run="All"),
            "violates PodSecurity",
        )
        host_path_pod = k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(name="baseline-host-path-pod"),
            spec=k8s_client.V1PodSpec(
                containers=[k8s_client.V1Container(name="fixture", image="example.invalid/unused:test")],
                volumes=[
                    k8s_client.V1Volume(name="host-path", host_path=k8s_client.V1HostPathVolumeSource(path="/tmp"))
                ],
            ),
        )
        assert await _is_rejected(
            core.create_namespaced_pod(security.TENANT_NAMESPACES[0], host_path_pod, dry_run="All"),
            "violates PodSecurity",
        )

        admission = k8s_client.AdmissionregistrationV1Api(server.api)
        await admission.create_validating_admission_policy(cast(Any, policy))
        await admission.create_validating_admission_policy_binding(cast(Any, binding))
        sacrificial_policy = {**policy, "metadata": {"name": "tenant-editable-sacrificial-policy"}}
        await admission.create_validating_admission_policy(cast(Any, sacrificial_policy))

        await admin_rbac.create_cluster_role(
            k8s_client.V1ClusterRole(
                metadata=k8s_client.V1ObjectMeta(name=_CLUSTER_ROLE),
                rules=[k8s_client.V1PolicyRule(verbs=["get"], resources=["pods"], api_groups=[""])],
            )
        )
        await admin_rbac.create_cluster_role_binding(_cluster_role_binding(_CLUSTER_ROLE_BINDING))

        # The cluster administrator remains able to update namespace-owned labels and RBAC.
        await cast(Any, core).patch_namespace(
            security.TENANT_NAMESPACES[0],
            {"metadata": {"labels": {"agentplane.ducktape.io/admin-test": "allowed"}}},
            dry_run="All",
            _content_type="application/merge-patch+json",
        )
        await cast(Any, admin_rbac).patch_cluster_role_binding(
            _CLUSTER_ROLE_BINDING,
            {"metadata": {"annotations": {"agentplane.ducktape.io/admin-test": "allowed"}}},
            dry_run="All",
            _content_type="application/merge-patch+json",
        )
        await admin_rbac.delete_cluster_role_binding(_CLUSTER_ROLE_BINDING, dry_run="All")

        async with await new_client_from_config(config_file=str(server.kubeconfig)) as impersonating:
            impersonating.set_default_header(
                "Impersonate-User", f"system:serviceaccount:{security.TENANT_NAMESPACES[0]}:{_SERVICE_ACCOUNT}"
            )
            # Bypass RBAC deliberately: admission must provide the separate scope guard.
            impersonating.set_default_header("Impersonate-Group", "system:masters")
            tenant_core = k8s_client.CoreV1Api(impersonating)
            tenant_rbac = k8s_client.RbacAuthorizationV1Api(impersonating)
            tenant_admission = k8s_client.AdmissionregistrationV1Api(impersonating)
            tenant_authentication = k8s_client.AuthenticationV1Api(impersonating)
            tenant_authorization = k8s_client.AuthorizationV1Api(impersonating)

            async def allowed_config_map(namespace: str, name: str) -> object:
                return await tenant_core.create_namespaced_config_map(
                    namespace,
                    k8s_client.V1ConfigMap(metadata=k8s_client.V1ObjectMeta(name=name), data={"safe": "true"}),
                    dry_run="All",
                )

            await _wait_for_policy(
                lambda: cast(Any, tenant_core).patch_namespace(
                    security.TENANT_NAMESPACES[0],
                    {"metadata": {"labels": {"pod-security.kubernetes.io/enforce": "privileged"}}},
                    dry_run="All",
                    _content_type="application/merge-patch+json",
                )
            )
            assert not await _is_policy_denial(allowed_config_map(security.TENANT_NAMESPACES[0], "tenant-safe"))

            # These create requests are review operations that return status instead of storing infrastructure.
            token_review = await tenant_authentication.create_token_review(
                k8s_client.V1TokenReview(spec=k8s_client.V1TokenReviewSpec(token="invalid-test-token-not-a-secret"))
            )
            assert token_review.status is not None
            assert token_review.status.authenticated is not True
            assert token_review.status.error
            access_review = await tenant_authorization.create_self_subject_access_review(
                k8s_client.V1SelfSubjectAccessReview(
                    spec=k8s_client.V1SelfSubjectAccessReviewSpec(
                        resource_attributes=k8s_client.V1ResourceAttributes(
                            group="", version="v1", resource="pods", verb="get", namespace=security.TENANT_NAMESPACES[0]
                        )
                    )
                )
            )
            assert access_review.status is not None
            assert access_review.status.allowed is True
            assert access_review.status.evaluation_error is None

            # Kubernetes does not apply VAP admission policy to writes to VAPs themselves. This
            # sacrificial policy proves that RBAC must continue to deny admission-configuration
            # writes to tenant identities, even if another grant gives them broad API authority.
            await cast(Any, tenant_admission).patch_validating_admission_policy(
                "tenant-editable-sacrificial-policy",
                {"metadata": {"annotations": {"tenant-test": "edited"}}},
                _content_type="application/merge-patch+json",
            )
            edited_sacrificial_policy = await admission.read_validating_admission_policy(
                "tenant-editable-sacrificial-policy"
            )
            assert edited_sacrificial_policy.metadata.annotations["tenant-test"] == "edited"
            await admission.delete_validating_admission_policy("tenant-editable-sacrificial-policy")

            # All three RBAC verbs are denied even though system:masters authorizes the requests.
            assert await _is_policy_denial(
                tenant_rbac.create_cluster_role_binding(_cluster_role_binding("tenant-created-binding"), dry_run="All")
            )
            assert await _is_policy_denial(
                cast(Any, tenant_rbac).patch_cluster_role_binding(
                    _CLUSTER_ROLE_BINDING,
                    {"metadata": {"annotations": {"tenant": "update"}}},
                    dry_run="All",
                    _content_type="application/merge-patch+json",
                )
            )
            assert await _is_policy_denial(
                tenant_rbac.delete_cluster_role_binding(_CLUSTER_ROLE_BINDING, dry_run="All")
            )

            secret = k8s_client.V1Secret(
                metadata=k8s_client.V1ObjectMeta(name="tenant-cross-namespace-secret"),
                string_data={"secret": "not-persisted"},
            )
            pod = k8s_client.V1Pod(
                metadata=k8s_client.V1ObjectMeta(name="tenant-cross-namespace-pod"),
                spec=k8s_client.V1PodSpec(
                    containers=[k8s_client.V1Container(name="fixture", image="example.invalid/unused:test")]
                ),
            )
            # The administrator can use the same cross-namespace operations.
            await core.create_namespaced_secret(_OTHER_NAMESPACE, secret, dry_run="All")
            await core.create_namespaced_pod(_OTHER_NAMESPACE, pod, dry_run="All")
            assert await _is_policy_denial(
                tenant_core.create_namespaced_secret(_OTHER_NAMESPACE, secret, dry_run="All")
            )
            assert await _is_policy_denial(tenant_core.create_namespaced_pod(_OTHER_NAMESPACE, pod, dry_run="All"))


if __name__ == "__main__":
    pytest_bazel.main()
