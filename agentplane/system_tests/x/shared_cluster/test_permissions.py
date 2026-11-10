from collections.abc import Iterable
from typing import cast

import pytest
import pytest_bazel
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio.client.models import RbacV1Subject

from agentplane.subjects import ServiceAccountRef
from agentplane.system_tests.x.shared_cluster import permissions
from agentplane.testing.kubernetes_apiserver import KubernetesApiServer, kubernetes_apiserver

type _PermissionKey = tuple[str, str, str | None, str, str | None, str | None]

_ALLOWED: set[_PermissionKey] = {
    ("authorization.k8s.io", "selfsubjectaccessreviews", None, "create", None, None),
    ("agentplane.allegedly.works", "sandboxes", None, "get", "sandbox-test", None),
    ("", "pods", "log", "get", "sandbox-test", None),
    ("kubevirt.io", "virtualmachineinstances", None, "get", "sandbox-test", None),
}


def _key(attributes: k8s.V1ResourceAttributes) -> _PermissionKey:
    return (
        attributes.group or "",
        attributes.resource,
        attributes.subresource,
        attributes.verb,
        attributes.namespace,
        attributes.name,
    )


class FakeApiClient:
    def __init__(
        self, *, allowed: set[_PermissionKey] | None = None, evaluation_errors: set[_PermissionKey] | None = None
    ) -> None:
        self.allowed = set() if allowed is None else allowed
        self.evaluation_errors = set() if evaluation_errors is None else evaluation_errors
        self.requests: list[_PermissionKey] = []


class FakeAuthorizationV1Api:
    def __init__(self, api_client: FakeApiClient) -> None:
        self.api_client = api_client

    async def create_self_subject_access_review(
        self, *, body: k8s.V1SelfSubjectAccessReview
    ) -> k8s.V1SelfSubjectAccessReview:
        assert body.spec is not None
        assert body.spec.resource_attributes is not None
        key = _key(body.spec.resource_attributes)
        self.api_client.requests.append(key)
        status = k8s.V1SubjectAccessReviewStatus(
            allowed=key in self.api_client.allowed,
            evaluation_error="incomplete" if key in self.api_client.evaluation_errors else None,
        )
        return k8s.V1SelfSubjectAccessReview(spec=body.spec, status=status)


def _evidence(
    evidence: Iterable[permissions.PermissionEvidence],
    *,
    resource: str,
    api_group: str,
    verb: str,
    namespace: str | None,
    subresource: str | None = None,
    name: str | None = None,
) -> permissions.PermissionEvidence:
    return next(
        item
        for item in evidence
        if (item.resource, item.api_group, item.verb, item.namespace, item.subresource, item.name)
        == (resource, api_group, verb, namespace, subresource, name)
    )


def _install_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permissions.k8s, "AuthorizationV1Api", FakeAuthorizationV1Api)


@pytest.mark.asyncio
async def test_minimal_observer_permissions_produce_diagnostic_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch)
    api_client = FakeApiClient(allowed=_ALLOWED)

    evidence = await permissions.check_permissions(api_client=cast(k8s.ApiClient, api_client), namespace="sandbox-test")

    assert _evidence(
        evidence, resource="sandboxes", api_group="agentplane.allegedly.works", verb="get", namespace="sandbox-test"
    ).allowed
    assert _evidence(
        evidence, resource="pods", api_group="", verb="get", namespace="sandbox-test", subresource="log"
    ).allowed
    assert _evidence(
        evidence, resource="virtualmachineinstances", api_group="kubevirt.io", verb="get", namespace="sandbox-test"
    ).allowed
    for denied in (
        ("pods", "", "create", "sandbox-test", None, None),
        ("pods", "", "create", "kubevirt", "exec", None),
        ("pods", "", "get", "kube-system", "attach", None),
        ("secrets", "", "get", "default", None, None),
        ("serviceaccounts", "", "create", "kube-system", "token", None),
        ("groups", "", "impersonate", None, None, "system:masters"),
        ("nodes", "", "get", None, "proxy", None),
        ("signers", "certificates.k8s.io", "approve", None, None, "kubernetes.io/kubelet-serving"),
    ):
        resource, api_group, verb, namespace, subresource, name = denied
        assert not _evidence(
            evidence,
            resource=resource,
            api_group=api_group,
            verb=verb,
            namespace=namespace,
            subresource=subresource,
            name=name,
        ).allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permission", "resource", "api_group", "verb", "namespace", "subresource", "name"),
    [
        (("", "pods", None, "create", "sandbox-test", None), "pods", "", "create", "sandbox-test", None, None),
        (("", "secrets", None, "get", "kube-system", None), "secrets", "", "get", "kube-system", None, None),
    ],
)
async def test_forbidden_permission_grant_fails_with_evidence(
    monkeypatch: pytest.MonkeyPatch,
    permission: _PermissionKey,
    resource: str,
    api_group: str,
    verb: str,
    namespace: str | None,
    subresource: str | None,
    name: str | None,
) -> None:
    _install_fake(monkeypatch)
    api_client = FakeApiClient(allowed=_ALLOWED | {permission})

    with pytest.raises(permissions.PermissionCheckError) as error:
        await permissions.check_permissions(api_client=cast(k8s.ApiClient, api_client), namespace="sandbox-test")

    violation = _evidence(
        error.value.evidence,
        resource=resource,
        api_group=api_group,
        verb=verb,
        namespace=namespace,
        subresource=subresource,
        name=name,
    )
    assert violation.allowed
    assert not violation.expected_allowed


@pytest.mark.asyncio
async def test_missing_required_read_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch)
    denied = ("", "pods", "log", "get", "sandbox-test", None)
    api_client = FakeApiClient(allowed=_ALLOWED - {denied})

    with pytest.raises(permissions.PermissionCheckError) as error:
        await permissions.check_permissions(api_client=cast(k8s.ApiClient, api_client), namespace="sandbox-test")

    evidence = _evidence(
        error.value.evidence, resource="pods", api_group="", verb="get", namespace="sandbox-test", subresource="log"
    )
    assert not evidence.allowed
    assert evidence.expected_allowed


@pytest.mark.asyncio
async def test_incomplete_review_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch)
    api_client = FakeApiClient(
        allowed=_ALLOWED,
        evaluation_errors={("authorization.k8s.io", "selfsubjectaccessreviews", None, "create", None, None)},
    )

    with pytest.raises(permissions.PermissionCheckError, match="incomplete authorization decision") as error:
        await permissions.check_permissions(api_client=cast(k8s.ApiClient, api_client), namespace="sandbox-test")

    assert error.value.evidence[0].allowed is None


async def _grant_observer_role(server: KubernetesApiServer, namespace: str) -> None:
    core = k8s.CoreV1Api(server.api)
    rbac = k8s.RbacAuthorizationV1Api(server.api)
    account = "observer"
    await core.create_namespace(k8s.V1Namespace(metadata=k8s.V1ObjectMeta(name=namespace)))
    await core.create_namespaced_service_account(
        namespace, k8s.V1ServiceAccount(metadata=k8s.V1ObjectMeta(name=account))
    )
    await rbac.create_namespaced_role(
        namespace,
        k8s.V1Role(
            metadata=k8s.V1ObjectMeta(name=account),
            rules=[
                k8s.V1PolicyRule(api_groups=["agentplane.allegedly.works"], resources=["sandboxes"], verbs=["get"]),
                k8s.V1PolicyRule(api_groups=[""], resources=["pods/log"], verbs=["get"]),
                k8s.V1PolicyRule(api_groups=["kubevirt.io"], resources=["virtualmachineinstances"], verbs=["get"]),
            ],
        ),
    )
    await rbac.create_namespaced_role_binding(
        namespace,
        k8s.V1RoleBinding(
            metadata=k8s.V1ObjectMeta(name=account),
            role_ref=k8s.V1RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=account),
            subjects=[RbacV1Subject(kind="ServiceAccount", name=account, namespace=namespace)],
        ),
    )
    await rbac.create_cluster_role(
        k8s.V1ClusterRole(
            metadata=k8s.V1ObjectMeta(name="observer-self-subject-access-review"),
            rules=[
                k8s.V1PolicyRule(
                    api_groups=["authorization.k8s.io"], resources=["selfsubjectaccessreviews"], verbs=["create"]
                )
            ],
        )
    )
    await rbac.create_cluster_role_binding(
        k8s.V1ClusterRoleBinding(
            metadata=k8s.V1ObjectMeta(name="observer-self-subject-access-review"),
            role_ref=k8s.V1RoleRef(
                api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="observer-self-subject-access-review"
            ),
            subjects=[RbacV1Subject(kind="ServiceAccount", name=account, namespace=namespace)],
        )
    )


def _observer_api(server: KubernetesApiServer, token: str) -> k8s.ApiClient:
    configuration = k8s.Configuration()
    configuration.host = server.api.configuration.host
    configuration.ssl_ca_cert = server.api.configuration.ssl_ca_cert
    configuration.verify_ssl = server.api.configuration.verify_ssl
    configuration.api_key = {"BearerToken": token}
    configuration.api_key_prefix = {"BearerToken": "Bearer"}
    return k8s.ApiClient(configuration)


@pytest.mark.asyncio
async def test_real_rbac_observer_and_wildcard_grant() -> None:
    namespace = "permission-check"
    async with kubernetes_apiserver() as server:
        await _grant_observer_role(server, namespace)
        issuer = server.api.configuration.host
        assert issuer is not None
        token = await server.workload_token(
            ServiceAccountRef(name="observer", namespace=namespace), audiences=[issuer.rstrip("/") + "/"]
        )
        async with _observer_api(server, token) as observer_api:
            evidence = await permissions.check_permissions(api_client=observer_api, namespace=namespace)
            assert (
                _evidence(evidence, resource="pods", api_group="", verb="create", namespace=namespace).allowed is False
            )
            assert (
                _evidence(
                    evidence, resource="pods", api_group="", verb="get", namespace=namespace, subresource="log"
                ).allowed
                is True
            )

            rbac = k8s.RbacAuthorizationV1Api(server.api)
            await rbac.create_cluster_role(
                k8s.V1ClusterRole(
                    metadata=k8s.V1ObjectMeta(name="observer-wildcard"),
                    rules=[k8s.V1PolicyRule(api_groups=["*"], resources=["*"], verbs=["*"])],
                )
            )
            await rbac.create_cluster_role_binding(
                k8s.V1ClusterRoleBinding(
                    metadata=k8s.V1ObjectMeta(name="observer-wildcard"),
                    role_ref=k8s.V1RoleRef(
                        api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="observer-wildcard"
                    ),
                    subjects=[RbacV1Subject(kind="ServiceAccount", name="observer", namespace=namespace)],
                )
            )

            with pytest.raises(permissions.PermissionCheckError) as error:
                await permissions.check_permissions(api_client=observer_api, namespace=namespace)

            assert (
                _evidence(
                    error.value.evidence, resource="pods", api_group="", verb="create", namespace=namespace
                ).allowed
                is True
            )


if __name__ == "__main__":
    pytest_bazel.main()
