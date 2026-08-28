"""Policy mapping and evaluation guard for kubectl-passthrough-mcp tool calls."""

from __future__ import annotations

import logging
from typing import Any

from haku.console.grants.kubernetes.authorization import AuthorizationRequest, RequestAttributes, required_rule
from haku.console.grants.kubernetes.models import (
    KubernetesAllNamespacesGrantScope,
    KubernetesClusterGrantScope,
    KubernetesNamespacesGrantScope,
)

logger = logging.getLogger(__name__)


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
    attributes = RequestAttributes(
        resource_request=True,
        verb=verb,
        api_group=api_group,
        api_version=version,
        namespace=namespace if not cluster_scoped else "",
        resource=resource,
        name=name or "",
        subresource=subresource or "",
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

            case "pods_list_in_namespace":
                namespace = arguments.get("namespace")
                if not namespace:
                    return None
                return [_make_req("list", "", "v1", "pods", namespace=namespace)]

            case "events_list":
                namespace = arguments.get("namespace")
                if not namespace:
                    return None
                return [_make_req("list", "", "v1", "events", namespace=namespace)]

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

            case "resources_list" | "resources_get" | "resources_delete" | "resources_create_or_update":
                # TODO: Support generic resource pluralization / CRD discovery without heuristic pluralization.
                return None

            case _:
                return None
    except Exception:
        logger.exception("Failed to map kubectl-passthrough tool %s", tool_name)
        return None
