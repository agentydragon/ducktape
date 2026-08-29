"""Console-side authorization for the Haku Kubernetes API proxy.

The proxy authenticates an Agent with a Haku bearer, but the Kubernetes
authorization decision is made by a Kubernetes SubjectAccessReview (SAR).  A
SAR is always issued for the deploy-configured subject below; request callers
cannot supply a Kubernetes username or groups.

The evaluator itself has no ambient request state. The bearer resolver and optional grant service
are supplied by the Console composition root; both the proxy and in-process MCP path feed the same
trusted Agent evaluation method. Kubernetes authorization is disabled when no config is supplied,
and every client/config failure is surfaced as an unavailable authority so callers fail closed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthorizationV1Api
from kubernetes_asyncio.config.config_exception import ConfigException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from haku.console.config import KubernetesAuthorizationSubject
from haku.console.grants.kubernetes.models import (
    AllNamespacesGrantScope,
    ClusterGrantScope,
    GrantScope,
    GrantScopeKind,
    NamespacesGrantScope,
    NonResourceGrantScope,
    Rule,
    validate_grant_scope_rules,
)
from haku.grants.authorization import AuthorizationUnavailableError

logger = logging.getLogger(__name__)


class KubernetesAuthorizationUnavailableError(AuthorizationUnavailableError):
    """The Console cannot make an authoritative Kubernetes decision."""


class KubernetesBearerRejectedError(RuntimeError):
    """The presented Haku bearer does not resolve to an active Agent."""


class RequestAttributes(BaseModel):
    """Kubernetes' canonical interpretation of one HTTP request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_request: bool
    verb: str = Field(min_length=1)
    api_group: str = ""
    api_version: str = ""
    namespace: str = Field(
        default="",
        description="Exact namespace of a namespaced request; empty for cluster-scoped resources "
        "and all-namespaces queries.",
    )
    resource: str = ""
    subresource: str = ""
    name: str = ""
    path: str = ""
    field_selector: str = ""
    label_selector: str = ""

    @model_validator(mode="after")
    def validate_path_for_non_resource(self) -> RequestAttributes:
        if not self.resource_request and not self.path:
            raise ValueError("non-resource requests require a non-empty path")
        return self


class AuthorizationRequest(BaseModel):
    """Proxy-to-Console authorization request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attributes: RequestAttributes
    required_scope: GrantScope
    required_rules: list[Rule] = Field(default_factory=list, min_length=1)

    @model_validator(mode="after")
    def validate_canonical_projection(self) -> AuthorizationRequest:
        validate_grant_scope_rules(self.required_scope, self.required_rules)
        if self.required_rules != [required_rule(self.attributes)]:
            raise ValueError("required_rules must be the canonical minimal rule for attributes")
        attributes = self.attributes
        scope = self.required_scope
        if not attributes.resource_request:
            if scope.kind is not GrantScopeKind.NON_RESOURCE:
                raise ValueError("a non-resource request requires non_resource scope")
        elif attributes.namespace:
            expected = NamespacesGrantScope(namespaces=(attributes.namespace,))
            if scope != expected:
                raise ValueError("a named-namespace request requires its exact namespace scope")
        elif scope.kind not in {GrantScopeKind.ALL_NAMESPACES, GrantScopeKind.CLUSTER}:
            raise ValueError("an unnamespaced resource request requires all_namespaces or cluster scope")
        return self


@dataclass(frozen=True, slots=True)
class SubjectAccessReviewResult:
    allowed: bool
    reason: str | None = None


class SubjectAccessReviewClient(Protocol):
    async def review(
        self, *, subject: KubernetesAuthorizationSubject, attributes: RequestAttributes
    ) -> SubjectAccessReviewResult: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KubernetesClients:
    api: ApiClient
    authorization: AuthorizationV1Api


class KubernetesSubjectAccessReviewClient:
    """Lazily connect to the in-cluster Kubernetes Authorization API."""

    def __init__(self, clients: KubernetesClients | None = None) -> None:
        self._clients = clients
        self._lock = asyncio.Lock()

    async def _connected(self) -> KubernetesClients:
        async with self._lock:
            if self._clients is None:
                configuration = k8s_client.Configuration()
                try:
                    k8s_config.load_incluster_config(client_configuration=configuration)
                except ConfigException as error:
                    raise KubernetesAuthorizationUnavailableError(
                        "Kubernetes in-cluster configuration is unavailable"
                    ) from error
                api = ApiClient(configuration=configuration)
                self._clients = KubernetesClients(api=api, authorization=AuthorizationV1Api(api))
            return self._clients

    async def review(
        self, *, subject: KubernetesAuthorizationSubject, attributes: RequestAttributes
    ) -> SubjectAccessReviewResult:
        resource_attributes = None
        non_resource_attributes = None
        if attributes.resource_request:
            resource_attributes = k8s_client.V1ResourceAttributes(
                group=attributes.api_group,
                version=attributes.api_version,
                namespace=attributes.namespace,
                resource=attributes.resource,
                subresource=attributes.subresource,
                name=attributes.name,
                verb=attributes.verb,
            )
        else:
            non_resource_attributes = k8s_client.V1NonResourceAttributes(path=attributes.path, verb=attributes.verb)
        request = k8s_client.V1SubjectAccessReview(
            api_version="authorization.k8s.io/v1",
            kind="SubjectAccessReview",
            spec=k8s_client.V1SubjectAccessReviewSpec(
                user=subject.username,
                groups=list(subject.groups),
                resource_attributes=resource_attributes,
                non_resource_attributes=non_resource_attributes,
            ),
        )
        try:
            response = await (await self._connected()).authorization.create_subject_access_review(request)
        except KubernetesAuthorizationUnavailableError:
            raise
        except Exception as error:
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization API is unavailable") from error
        status = response.status
        if status is None or status.allowed is None:
            raise KubernetesAuthorizationUnavailableError("Kubernetes returned an incomplete authorization response")
        if getattr(status, "evaluation_error", None):
            raise KubernetesAuthorizationUnavailableError("Kubernetes authorization evaluation reported an error")
        if status.allowed and getattr(status, "denied", False):
            raise KubernetesAuthorizationUnavailableError("Kubernetes returned a contradictory authorization response")
        return SubjectAccessReviewResult(allowed=bool(status.allowed), reason=status.reason)

    async def aclose(self) -> None:
        if self._clients is not None:
            await self._clients.api.close()
            self._clients = None


def required_rule(attributes: RequestAttributes) -> Rule:
    """Build the minimal RBAC rule; Kubernetes spells subresources as ``resource/subresource``."""

    if not attributes.resource_request:
        return Rule(verbs=(attributes.verb,), non_resource_urls=(attributes.path,))
    resource = attributes.resource
    if attributes.subresource:
        resource = f"{resource}/{attributes.subresource}"
    return Rule(
        api_groups=(attributes.api_group,),
        resources=(resource,),
        verbs=(attributes.verb,),
        resource_names=(attributes.name,) if attributes.name else (),
    )


# Reviewed static set of built-in kinds that are cluster-scoped in stock Kubernetes, keyed by
# (api_group, resource) because a CRD may reuse a resource name in another group (nodes.longhorn.io
# is namespaced). Kinds outside this set — CRDs and unknowns — must still declare their scope.
BUILTIN_CLUSTER_SCOPED_RESOURCES: frozenset[tuple[str, str]] = frozenset(
    {
        ("", "namespaces"),
        ("", "nodes"),
        ("", "persistentvolumes"),
        ("admissionregistration.k8s.io", "mutatingwebhookconfigurations"),
        ("admissionregistration.k8s.io", "validatingadmissionpolicies"),
        ("admissionregistration.k8s.io", "validatingadmissionpolicybindings"),
        ("admissionregistration.k8s.io", "validatingwebhookconfigurations"),
        ("apiextensions.k8s.io", "customresourcedefinitions"),
        ("apiregistration.k8s.io", "apiservices"),
        ("certificates.k8s.io", "certificatesigningrequests"),
        ("flowcontrol.apiserver.k8s.io", "flowschemas"),
        ("flowcontrol.apiserver.k8s.io", "prioritylevelconfigurations"),
        ("networking.k8s.io", "ingressclasses"),
        ("node.k8s.io", "runtimeclasses"),
        ("rbac.authorization.k8s.io", "clusterrolebindings"),
        ("rbac.authorization.k8s.io", "clusterroles"),
        ("scheduling.k8s.io", "priorityclasses"),
        ("storage.k8s.io", "csidrivers"),
        ("storage.k8s.io", "csinodes"),
        ("storage.k8s.io", "storageclasses"),
        ("storage.k8s.io", "volumeattachments"),
    }
)


def required_scope(
    attributes: RequestAttributes, *, unnamespaced_resource_kind: GrantScopeKind | None = None
) -> GrantScope:
    """Derive scope from a named namespace, a declared unnamespaced kind, or a built-in cluster-scoped kind."""

    if not attributes.resource_request:
        if unnamespaced_resource_kind is not None:
            raise ValueError("non-resource requests cannot declare a resource scope kind")
        return NonResourceGrantScope()
    if attributes.namespace:
        if unnamespaced_resource_kind is not None:
            raise ValueError("a named-namespace request cannot declare an unnamespaced resource scope kind")
        return NamespacesGrantScope(namespaces=(attributes.namespace,))
    if unnamespaced_resource_kind is GrantScopeKind.ALL_NAMESPACES:
        return AllNamespacesGrantScope()
    if unnamespaced_resource_kind is GrantScopeKind.CLUSTER:
        return ClusterGrantScope()
    if unnamespaced_resource_kind is not None:
        raise ValueError(
            f"unnamespaced_resource_kind must be 'all_namespaces' or 'cluster', not {unnamespaced_resource_kind}"
        )
    if (attributes.api_group, attributes.resource) in BUILTIN_CLUSTER_SCOPED_RESOURCES:
        return ClusterGrantScope()
    resource = f"{attributes.resource}.{attributes.api_group}" if attributes.api_group else attributes.resource
    raise ValueError(
        f"cannot infer the scope of unnamespaced resource {resource!r}: "
        "declare unnamespaced_resource_kind='all_namespaces' or 'cluster'"
    )
