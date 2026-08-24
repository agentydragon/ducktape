"""Policy mapping and evaluation guard for kubectl-passthrough-mcp tool calls."""

from __future__ import annotations

import logging
from typing import Any

import yaml

from haku.console.kubernetes_authorization import AuthorizationRequest, RequestAttributes, required_rule
from haku.console.kubernetes_grant_models import (
    KubernetesAllNamespacesGrantScope,
    KubernetesClusterGrantScope,
    KubernetesNamespacesGrantScope,
)

logger = logging.getLogger(__name__)


def map_kubectl_passthrough_request(tool_name: str, arguments: dict[str, Any]) -> list[AuthorizationRequest] | None:
    """Map a kubectl-passthrough-mcp tool name and arguments to canonical AuthorizationRequests.

    Returns None if the tool or arguments are unmappable or unknown (safe fallback to approval).
    """
    try:
        match tool_name:
            case "pods_list":
                namespace = arguments.get("namespace", "")
                scope = (
                    KubernetesNamespacesGrantScope(namespaces=(namespace,))
                    if namespace
                    else KubernetesAllNamespacesGrantScope()
                )
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="list",
                    api_group="",
                    api_version="v1",
                    namespace=namespace,
                    resource="pods",
                    path=f"/api/v1/namespaces/{namespace}/pods" if namespace else "/api/v1/pods",
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "pods_get":
                name = arguments.get("name")
                if not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                scope = KubernetesNamespacesGrantScope(namespaces=(namespace,))
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="get",
                    api_group="",
                    api_version="v1",
                    namespace=namespace,
                    resource="pods",
                    name=name,
                    path=f"/api/v1/namespaces/{namespace}/pods/{name}",
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "pods_delete":
                name = arguments.get("name")
                if not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                scope = KubernetesNamespacesGrantScope(namespaces=(namespace,))
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="delete",
                    api_group="",
                    api_version="v1",
                    namespace=namespace,
                    resource="pods",
                    name=name,
                    path=f"/api/v1/namespaces/{namespace}/pods/{name}",
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "pods_log":
                name = arguments.get("name")
                if not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                scope = KubernetesNamespacesGrantScope(namespaces=(namespace,))
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="get",
                    api_group="",
                    api_version="v1",
                    namespace=namespace,
                    resource="pods",
                    subresource="log",
                    name=name,
                    path=f"/api/v1/namespaces/{namespace}/pods/{name}/log",
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "pods_exec":
                name = arguments.get("name")
                if not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                scope = KubernetesNamespacesGrantScope(namespaces=(namespace,))
                exec_attr = RequestAttributes(
                    resource_request=True,
                    verb="create",
                    api_group="",
                    api_version="v1",
                    namespace=namespace,
                    resource="pods",
                    subresource="exec",
                    name=name,
                    path=f"/api/v1/namespaces/{namespace}/pods/{name}/exec",
                )
                get_attr = RequestAttributes(
                    resource_request=True,
                    verb="get",
                    api_group="",
                    api_version="v1",
                    namespace=namespace,
                    resource="pods",
                    name=name,
                    path=f"/api/v1/namespaces/{namespace}/pods/{name}",
                )
                return [
                    AuthorizationRequest(
                        attributes=exec_attr, required_scope=scope, required_rules=[required_rule(exec_attr)]
                    ),
                    AuthorizationRequest(
                        attributes=get_attr, required_scope=scope, required_rules=[required_rule(get_attr)]
                    ),
                ]

            case "nodes_list":
                scope = KubernetesClusterGrantScope()
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="list",
                    api_group="",
                    api_version="v1",
                    resource="nodes",
                    path="/api/v1/nodes",
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "nodes_get":
                name = arguments.get("name")
                if not name:
                    return None
                scope = KubernetesClusterGrantScope()
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="get",
                    api_group="",
                    api_version="v1",
                    resource="nodes",
                    name=name,
                    path=f"/api/v1/nodes/{name}",
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "resources_list":
                api_version = arguments.get("apiVersion")
                kind = arguments.get("kind")
                if not api_version or not kind:
                    return None
                namespace = arguments.get("namespace", "")
                api_group, _, version = api_version.partition("/")
                if not version:
                    version = api_group
                    api_group = ""
                resource_plural = _kind_to_plural(kind)
                scope = (
                    KubernetesNamespacesGrantScope(namespaces=(namespace,))
                    if namespace
                    else KubernetesAllNamespacesGrantScope()
                )
                path = (
                    (
                        f"/apis/{api_group}/{version}/namespaces/{namespace}/{resource_plural}"
                        if api_group
                        else f"/api/{version}/namespaces/{namespace}/{resource_plural}"
                    )
                    if namespace
                    else (
                        f"/apis/{api_group}/{version}/{resource_plural}"
                        if api_group
                        else f"/api/{version}/{resource_plural}"
                    )
                )
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="list",
                    api_group=api_group,
                    api_version=version,
                    namespace=namespace,
                    resource=resource_plural,
                    path=path,
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "resources_get":
                api_version = arguments.get("apiVersion")
                kind = arguments.get("kind")
                name = arguments.get("name")
                if not api_version or not kind or not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                api_group, _, version = api_version.partition("/")
                if not version:
                    version = api_group
                    api_group = ""
                resource_plural = _kind_to_plural(kind)
                scope = KubernetesNamespacesGrantScope(namespaces=(namespace,))
                path = (
                    f"/apis/{api_group}/{version}/namespaces/{namespace}/{resource_plural}/{name}"
                    if api_group
                    else f"/api/{version}/namespaces/{namespace}/{resource_plural}/{name}"
                )
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="get",
                    api_group=api_group,
                    api_version=version,
                    namespace=namespace,
                    resource=resource_plural,
                    name=name,
                    path=path,
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "resources_delete":
                api_version = arguments.get("apiVersion")
                kind = arguments.get("kind")
                name = arguments.get("name")
                if not api_version or not kind or not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                api_group, _, version = api_version.partition("/")
                if not version:
                    version = api_group
                    api_group = ""
                resource_plural = _kind_to_plural(kind)
                scope = KubernetesNamespacesGrantScope(namespaces=(namespace,))
                path = (
                    f"/apis/{api_group}/{version}/namespaces/{namespace}/{resource_plural}/{name}"
                    if api_group
                    else f"/api/{version}/namespaces/{namespace}/{resource_plural}/{name}"
                )
                attributes = RequestAttributes(
                    resource_request=True,
                    verb="delete",
                    api_group=api_group,
                    api_version=version,
                    namespace=namespace,
                    resource=resource_plural,
                    name=name,
                    path=path,
                )
                return [
                    AuthorizationRequest(
                        attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)]
                    )
                ]

            case "resources_create_or_update":
                raw_resource = arguments.get("resource")
                if not isinstance(raw_resource, str):
                    return None
                manifest = yaml.safe_load(raw_resource)
                if not isinstance(manifest, dict):
                    return None
                api_version = manifest.get("apiVersion")
                kind = manifest.get("kind")
                metadata = manifest.get("metadata", {})
                name = metadata.get("name")
                namespace = metadata.get("namespace") or "default"
                if not api_version or not kind or not name:
                    return None

                api_group, _, version = api_version.partition("/")
                if not version:
                    version = api_group
                    api_group = ""
                resource_plural = _kind_to_plural(kind)
                scope = KubernetesNamespacesGrantScope(namespaces=(namespace,))
                path = (
                    f"/apis/{api_group}/{version}/namespaces/{namespace}/{resource_plural}/{name}"
                    if api_group
                    else f"/api/{version}/namespaces/{namespace}/{resource_plural}/{name}"
                )
                collection_path = (
                    f"/apis/{api_group}/{version}/namespaces/{namespace}/{resource_plural}"
                    if api_group
                    else f"/api/{version}/namespaces/{namespace}/{resource_plural}"
                )

                get_attr = RequestAttributes(
                    resource_request=True,
                    verb="get",
                    api_group=api_group,
                    api_version=version,
                    namespace=namespace,
                    resource=resource_plural,
                    name=name,
                    path=path,
                )
                create_attr = RequestAttributes(
                    resource_request=True,
                    verb="create",
                    api_group=api_group,
                    api_version=version,
                    namespace=namespace,
                    resource=resource_plural,
                    path=collection_path,
                )
                patch_attr = RequestAttributes(
                    resource_request=True,
                    verb="patch",
                    api_group=api_group,
                    api_version=version,
                    namespace=namespace,
                    resource=resource_plural,
                    name=name,
                    path=path,
                )
                return [
                    AuthorizationRequest(
                        attributes=get_attr, required_scope=scope, required_rules=[required_rule(get_attr)]
                    ),
                    AuthorizationRequest(
                        attributes=create_attr, required_scope=scope, required_rules=[required_rule(create_attr)]
                    ),
                    AuthorizationRequest(
                        attributes=patch_attr, required_scope=scope, required_rules=[required_rule(patch_attr)]
                    ),
                ]

            case _:
                return None
    except Exception:
        logger.exception("Failed to map kubectl-passthrough tool %s", tool_name)
        return None


def _kind_to_plural(kind: str) -> str:
    k = kind.lower()
    if k.endswith("y") and not k.endswith(("ay", "ey", "iy", "oy", "uy")):
        return k[:-1] + "ies"
    if k.endswith(("s", "sh", "ch", "x", "z")):
        return k + "es"
    return k + "s"
