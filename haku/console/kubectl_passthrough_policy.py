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


def _parse_api_version(api_version: str) -> tuple[str, str]:
    api_group, _, version = api_version.partition("/")
    if not version:
        return "", api_group
    return api_group, version


def _build_path(
    api_group: str,
    version: str,
    resource: str,
    namespace: str | None = None,
    name: str | None = None,
    subresource: str | None = None,
) -> str:
    prefix = f"/apis/{api_group}/{version}" if api_group else f"/api/{version}"
    if namespace:
        prefix = f"{prefix}/namespaces/{namespace}"
    path = f"{prefix}/{resource}"
    if name:
        path = f"{path}/{name}"
    if subresource:
        path = f"{path}/{subresource}"
    return path


def _make_req(
    verb: str,
    api_group: str,
    version: str,
    resource: str,
    namespace: str | None = None,
    name: str | None = None,
    cluster_scoped: bool = False,
    subresource: str | None = None,
) -> AuthorizationRequest:
    scope = (
        KubernetesClusterGrantScope()
        if cluster_scoped
        else (
            KubernetesNamespacesGrantScope(namespaces=(namespace,))
            if namespace
            else KubernetesAllNamespacesGrantScope()
        )
    )
    path = _build_path(
        api_group,
        version,
        resource,
        namespace=namespace if not cluster_scoped else None,
        name=name,
        subresource=subresource,
    )
    attributes = RequestAttributes(
        resource_request=True,
        verb=verb,
        api_group=api_group,
        api_version=version,
        namespace=namespace if not cluster_scoped else "",
        resource=resource,
        name=name or "",
        subresource=subresource or "",
        path=path,
    )
    return AuthorizationRequest(attributes=attributes, required_scope=scope, required_rules=[required_rule(attributes)])


def map_kubectl_passthrough_request(tool_name: str, arguments: dict[str, Any]) -> list[AuthorizationRequest] | None:
    """Map a kubectl-passthrough-mcp tool name and arguments to canonical AuthorizationRequests.

    Returns None if the tool or arguments are unmappable or unknown (safe fallback to approval).
    """
    try:
        match tool_name:
            case "pods_list":
                namespace = arguments.get("namespace", "")
                return [_make_req("list", "", "v1", "pods", namespace=namespace)]

            case "pods_get" | "pods_delete" | "pods_log":
                name = arguments.get("name")
                if not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                verb = "get" if tool_name in ("pods_get", "pods_log") else "delete"
                subresource = "log" if tool_name == "pods_log" else None
                return [_make_req(verb, "", "v1", "pods", namespace=namespace, name=name, subresource=subresource)]

            case "pods_exec":
                name = arguments.get("name")
                if not name:
                    return None
                namespace = arguments.get("namespace") or "default"
                return [
                    _make_req("create", "", "v1", "pods", namespace=namespace, name=name, subresource="exec"),
                    _make_req("get", "", "v1", "pods", namespace=namespace, name=name),
                ]

            case "nodes_list":
                return [_make_req("list", "", "v1", "nodes", cluster_scoped=True)]

            case "nodes_get":
                name = arguments.get("name")
                if not name:
                    return None
                return [_make_req("get", "", "v1", "nodes", name=name, cluster_scoped=True)]

            case "resources_list" | "resources_get" | "resources_delete":
                api_version = arguments.get("apiVersion")
                kind = arguments.get("kind")
                if not api_version or not kind:
                    return None
                name = arguments.get("name")
                if tool_name in ("resources_get", "resources_delete") and not name:
                    return None

                api_group, version = _parse_api_version(api_version)
                resource_plural = _kind_to_plural(kind)
                namespace = (
                    arguments.get("namespace", "")
                    if tool_name == "resources_list"
                    else (arguments.get("namespace") or "default")
                )
                verb = (
                    "list" if tool_name == "resources_list" else ("get" if tool_name == "resources_get" else "delete")
                )
                return [_make_req(verb, api_group, version, resource_plural, namespace=namespace, name=name)]

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

                api_group, version = _parse_api_version(api_version)
                resource_plural = _kind_to_plural(kind)
                return [
                    _make_req("get", api_group, version, resource_plural, namespace=namespace, name=name),
                    _make_req("create", api_group, version, resource_plural, namespace=namespace),
                    _make_req("patch", api_group, version, resource_plural, namespace=namespace, name=name),
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
