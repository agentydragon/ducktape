"""The cluster's shared Gateway API `Gateway` -- every generated `HttpRoute` in this
repo attaches to it.
"""

from __future__ import annotations

from gateway_api_crds.io.k8s.networking.gateway import HttpRouteSpecParentRefs

_NAME = "cluster-gateway"
_NAMESPACE = "gateway-system"


def cluster_gateway_parent_ref(*, section_name: str | None = None) -> HttpRouteSpecParentRefs:
    return HttpRouteSpecParentRefs(name=_NAME, namespace=_NAMESPACE, section_name=section_name)
