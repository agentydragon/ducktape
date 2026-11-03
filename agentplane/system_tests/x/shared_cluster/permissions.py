"""Observe a shared-cluster test client's Kubernetes authorization boundary."""

import asyncio
from dataclasses import dataclass

from kubernetes_asyncio import client as k8s

_WRITE_VERBS = ("create", "update", "patch", "delete", "deletecollection")
_READ_VERBS = ("get", "list", "watch")
_CROSS_NAMESPACE_NAMES = ("default", "kube-system", "kubevirt")
_KUBEVIRT_NAMESPACE = "kubevirt"
_CSR_SIGNERS = (
    "kubernetes.io/kube-apiserver-client",
    "kubernetes.io/kube-apiserver-client-kubelet",
    "kubernetes.io/kubelet-client",
    "kubernetes.io/kubelet-serving",
    "kubernetes.io/legacy-unknown",
)
_MAX_CONCURRENT_REVIEWS = 8


@dataclass(frozen=True)
class _Requirement:
    resource: str
    api_group: str
    verb: str
    namespace: str | None
    expected_allowed: bool
    subresource: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class PermissionEvidence:
    resource: str
    api_group: str
    verb: str
    namespace: str | None
    allowed: bool | None
    expected_allowed: bool
    subresource: str | None = None
    name: str | None = None


class PermissionCheckError(Exception):
    """The API server returned an unexpected or incomplete authorization decision."""

    def __init__(self, message: str, *, evidence: tuple[PermissionEvidence, ...]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _reviews(
    resource: str,
    api_group: str,
    namespace: str | None,
    *,
    verbs: tuple[str, ...] = _WRITE_VERBS,
    subresource: str | None = None,
    name: str | None = None,
) -> tuple[_Requirement, ...]:
    return tuple(
        _Requirement(
            resource=resource,
            api_group=api_group,
            verb=verb,
            namespace=namespace,
            expected_allowed=False,
            subresource=subresource,
            name=name,
        )
        for verb in verbs
    )


def _requirements(namespace: str) -> tuple[_Requirement, ...]:
    scoped_namespaces = tuple(dict.fromkeys((namespace, *_CROSS_NAMESPACE_NAMES)))
    other_namespaces = tuple(name for name in scoped_namespaces if name != namespace)
    requirements = [
        _Requirement("selfsubjectaccessreviews", "authorization.k8s.io", "create", None, expected_allowed=True),
        _Requirement("sandboxes", "agentplane.allegedly.works", "get", namespace, expected_allowed=True),
        _Requirement("pods", "", "get", namespace, expected_allowed=True, subresource="log"),
        _Requirement("virtualmachineinstances", "kubevirt.io", "get", namespace, expected_allowed=True),
    ]

    for scoped_namespace in scoped_namespaces:
        requirements.extend(_reviews("pods", "", scoped_namespace))
        requirements.extend(_reviews("pods", "", scoped_namespace, verbs=("create", "get"), subresource="exec"))
        requirements.extend(_reviews("pods", "", scoped_namespace, verbs=("create", "get"), subresource="attach"))
        requirements.extend(_reviews("serviceaccounts", "", scoped_namespace))
        requirements.extend(_reviews("serviceaccounts", "", scoped_namespace, verbs=("create",), subresource="token"))
        requirements.extend(_reviews("secrets", "", scoped_namespace, verbs=(*_READ_VERBS, *_WRITE_VERBS)))
        requirements.extend(_reviews("roles", "rbac.authorization.k8s.io", scoped_namespace))
        requirements.extend(_reviews("rolebindings", "rbac.authorization.k8s.io", scoped_namespace))
        requirements.extend(_reviews("policies", "kyverno.io", scoped_namespace))
        requirements.extend(_reviews("serviceaccounts", "", scoped_namespace, verbs=("impersonate",), name="default"))

    for other_namespace in other_namespaces:
        requirements.extend(
            (
                _Requirement("sandboxes", "agentplane.allegedly.works", "get", other_namespace, False),
                _Requirement("pods", "", "get", other_namespace, False, subresource="log"),
                _Requirement("virtualmachines", "kubevirt.io", "get", other_namespace, False),
                _Requirement("virtualmachineinstances", "kubevirt.io", "get", other_namespace, False),
                *_reviews("serviceaccounts", "", other_namespace, verbs=_READ_VERBS),
            )
        )
    for resource, name in (("users", "system:admin"), ("groups", "system:masters"), ("groups", "system:nodes")):
        requirements.extend(_reviews(resource, "", None, verbs=("impersonate",), name=name))

    requirements.extend(_reviews("namespaces", "", None))
    requirements.extend(_reviews("customresourcedefinitions", "apiextensions.k8s.io", None))
    requirements.extend(_reviews("nodes", "", None))
    requirements.extend(
        _reviews("nodes", "", None, verbs=("get", "create", "update", "patch", "delete"), subresource="proxy")
    )
    requirements.extend(_reviews("persistentvolumes", "", None))
    requirements.extend(_reviews("storageclasses", "storage.k8s.io", None))
    for resource in ("clusterroles", "clusterrolebindings"):
        requirements.extend(_reviews(resource, "rbac.authorization.k8s.io", None))
    for resource in ("roles", "clusterroles"):
        resource_namespace = namespace if resource == "roles" else None
        requirements.extend(
            _reviews(resource, "rbac.authorization.k8s.io", resource_namespace, verbs=("bind", "escalate"))
        )
    requirements.extend(_reviews("kubevirts", "kubevirt.io", _KUBEVIRT_NAMESPACE))
    requirements.extend(_reviews("clusterpolicies", "kyverno.io", None))
    for resource in (
        "validatingadmissionpolicies",
        "validatingadmissionpolicybindings",
        "validatingwebhookconfigurations",
        "mutatingwebhookconfigurations",
    ):
        requirements.extend(_reviews(resource, "admissionregistration.k8s.io", None))
    requirements.extend(_reviews("certificatesigningrequests", "certificates.k8s.io", None))
    requirements.extend(
        _reviews(
            "certificatesigningrequests", "certificates.k8s.io", None, verbs=("update", "patch"), subresource="approval"
        )
    )
    for signer in _CSR_SIGNERS:
        requirements.extend(_reviews("signers", "certificates.k8s.io", None, verbs=("approve", "sign"), name=signer))
    return tuple(requirements)


def _attributes(requirement: _Requirement) -> k8s.V1ResourceAttributes:
    return k8s.V1ResourceAttributes(
        group=requirement.api_group,
        resource=requirement.resource,
        subresource=requirement.subresource,
        verb=requirement.verb,
        namespace=requirement.namespace,
        name=requirement.name,
    )


def _evidence(requirement: _Requirement, allowed: bool | None) -> PermissionEvidence:
    return PermissionEvidence(
        resource=requirement.resource,
        api_group=requirement.api_group,
        verb=requirement.verb,
        namespace=requirement.namespace,
        allowed=allowed,
        expected_allowed=requirement.expected_allowed,
        subresource=requirement.subresource,
        name=requirement.name,
    )


async def check_permissions(*, api_client: k8s.ApiClient, namespace: str) -> tuple[PermissionEvidence, ...]:
    """Review a supplied client's bounded shared-cluster authorization surface.

    These `SelfSubjectAccessReview` POSTs observe current authorization; they do not write the
    reviewed resources. Namespaced checks cover the supplied namespace and selected shared
    namespaces, not every namespace or every resource name. This is diagnostic evidence, not proof
    of admission behavior or a complete authorization audit.
    """
    if not namespace or namespace.strip() != namespace:
        raise ValueError("namespace must be a non-empty namespace name")

    authorization = k8s.AuthorizationV1Api(api_client)
    requirements = _requirements(namespace)
    results: list[PermissionEvidence | None] = [None] * len(requirements)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_REVIEWS)

    async def review(index: int, requirement: _Requirement) -> None:
        async with semaphore:
            response = await authorization.create_self_subject_access_review(
                body=k8s.V1SelfSubjectAccessReview(
                    spec=k8s.V1SelfSubjectAccessReviewSpec(resource_attributes=_attributes(requirement))
                )
            )
        status = response.status
        allowed = None if status is None or status.evaluation_error else status.allowed
        results[index] = _evidence(requirement, allowed=allowed)

    async with asyncio.TaskGroup() as tasks:
        for index, requirement in enumerate(requirements):
            tasks.create_task(review(index, requirement))

    evidence = tuple(item for item in results if item is not None)
    if len(evidence) != len(requirements) or any(item.allowed is None for item in evidence):
        raise PermissionCheckError(
            "SelfSubjectAccessReview returned an incomplete authorization decision", evidence=evidence
        )

    unexpected = tuple(item for item in evidence if item.allowed != item.expected_allowed)
    if unexpected:
        raise PermissionCheckError(
            f"Kubernetes permission boundary has {len(unexpected)} unexpected authorization decisions",
            evidence=evidence,
        )
    return evidence
