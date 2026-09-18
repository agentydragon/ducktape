"""The cluster's shared Gateway API `Gateway` -- every generated `HttpRoute` in this
repo attaches to it.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata
from constructs import Construct
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecParentRefs,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
    HttpRouteSpecRulesMatches,
    HttpRouteSpecRulesMatchesPath,
    HttpRouteSpecRulesMatchesPathType,
    HttpRouteSpecRulesTimeouts,
)

_NAME = "cluster-gateway"
_NAMESPACE = "gateway-system"
# Not the plaintext listener: the gateway's HTTP-only route owns port 80 and redirects it.
HTTPS_LISTENER = "https-wildcard"


def cluster_gateway_parent_ref(*, section_name: str | None = None) -> HttpRouteSpecParentRefs:
    return HttpRouteSpecParentRefs(name=_NAME, namespace=_NAMESPACE, section_name=section_name)


def https_route(
    scope: Construct,
    id: str,
    *,
    metadata: ApiObjectMetadata,
    hostname: str,
    backend: str,
    port: int,
    paths: Sequence[str] = (),
    timeout: str | None = None,
    hsts: bool = True,
    listener: str | None = HTTPS_LISTENER,
) -> HttpRoute:
    """`hostname` on the shared Gateway to one Service port. `paths` restricts the route to those
    exact paths; `hsts` sets Strict-Transport-Security at the TLS-aware edge, which the backend's
    own hop cannot see was HTTPS; `timeout` bounds the request and the backend request alike."""
    return HttpRoute(
        scope,
        id,
        metadata=metadata,
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref(section_name=listener)],
            hostnames=[hostname],
            rules=[
                HttpRouteSpecRules(
                    matches=[
                        HttpRouteSpecRulesMatches(
                            path=HttpRouteSpecRulesMatchesPath(type=HttpRouteSpecRulesMatchesPathType.EXACT, value=path)
                        )
                        for path in paths
                    ]
                    or None,
                    filters=[
                        HttpRouteSpecRulesFilters(
                            type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
                            response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                                set=[
                                    HttpRouteSpecRulesFiltersResponseHeaderModifierSet(
                                        name="Strict-Transport-Security", value="max-age=31536000"
                                    )
                                ]
                            ),
                        )
                    ]
                    if hsts
                    else None,
                    backend_refs=[HttpRouteSpecRulesBackendRefs(name=backend, port=port)],
                    timeouts=HttpRouteSpecRulesTimeouts(request=timeout, backend_request=timeout) if timeout else None,
                )
            ],
        ),
    )
