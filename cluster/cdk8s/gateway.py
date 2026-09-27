"""The cluster's shared Gateway API `Gateway` -- every generated `HttpRoute` in this
repo attaches to it -- with its `gateway-system` Namespace, the plaintext listener's
redirect to HTTPS, and the directory's Flux Kustomization.
"""

from __future__ import annotations

from collections.abc import Sequence

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecParentRefs,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersRequestRedirectScheme,
    HttpRouteSpecRulesFiltersRequestRedirectStatusCode,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesTimeouts,
)
from gateway_api_gateway_crds.io.k8s.networking.gateway import (
    Gateway,
    GatewaySpec,
    GatewaySpecListenersAllowedRoutes,
    GatewaySpecListenersAllowedRoutesNamespaces,
    GatewaySpecListenersAllowedRoutesNamespacesFrom,
)

from cluster.cdk8s.cert_manager.config import LETSENCRYPT_ISSUER
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.gateway_api.http_route import RouteFilter, RouteMatch
from cluster.cdk8s.providers.gateway_api.listener import Listener, ListenerTls

_NAME = "cluster-gateway"
_NAMESPACE = "gateway-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/gateway"
# Not the plaintext listener: the gateway's HTTP-only route owns port 80 and redirects it.
HTTPS_LISTENER = "https-wildcard"
_HTTP_LISTENER = "http"


def cluster_gateway_parent_ref(*, section_name: str | None = None) -> HttpRouteSpecParentRefs:
    return HttpRouteSpecParentRefs(name=_NAME, namespace=_NAMESPACE, section_name=section_name)


def https_route(
    scope: Construct,
    id: str,
    *,
    metadata: ApiObjectMetadata,
    hostnames: Sequence[str],
    backend: str,
    port: int,
    paths: Sequence[str] = (),
    path_prefix: str | None = None,
    timeout: str | None = None,
    hsts: bool = True,
    listener: str | None = HTTPS_LISTENER,
    extra_filters: Sequence[HttpRouteSpecRulesFilters] = (),
) -> HttpRoute:
    """`hostnames` on the shared Gateway to one Service port. `paths` restricts the route to those
    exact paths; `path_prefix` adds a path-prefix match alongside them. `hsts` sets
    Strict-Transport-Security at the TLS-aware edge, which the backend's own hop cannot see was
    HTTPS; `extra_filters` appends further filters after the HSTS one (when `hsts` is set).
    `timeout` bounds the request and the backend request alike."""
    matches = [RouteMatch.path_exact(path) for path in paths]
    if path_prefix is not None:
        matches.append(RouteMatch.path_prefix(path_prefix))
    filters = list(extra_filters)
    if hsts:
        filters.insert(
            0,
            RouteFilter.response_header_modifier(
                set=[
                    HttpRouteSpecRulesFiltersResponseHeaderModifierSet(
                        name="Strict-Transport-Security", value="max-age=31536000"
                    )
                ]
            ),
        )
    return HttpRoute(
        scope,
        id,
        metadata=metadata,
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref(section_name=listener)],
            hostnames=list(hostnames),
            rules=[
                HttpRouteSpecRules(
                    matches=matches or None,
                    filters=filters or None,
                    backend_refs=[HttpRouteSpecRulesBackendRefs(name=backend, port=port)],
                    timeouts=HttpRouteSpecRulesTimeouts(request=timeout, backend_request=timeout) if timeout else None,
                )
            ],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, "gateway-system", disable_resource_name_hashes=True)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=_NAMESPACE))
    # Every listener admits routes from all namespaces. Per-listener Selector restriction is
    # avoided because Cilium bug #42159 makes listener-scoped allowedRoutes config bleed
    # across listeners (see cluster/docs/plan.md). The fence against agents publishing
    # public routes that bypass Authentik is instead the Kyverno ClusterPolicy
    # `restrict-agent-gateway-routes`, which denies route creation in the agent sandbox
    # namespaces.
    all_namespaces = GatewaySpecListenersAllowedRoutes(
        namespaces=GatewaySpecListenersAllowedRoutesNamespaces(
            from_=GatewaySpecListenersAllowedRoutesNamespacesFrom.ALL
        )
    )
    Gateway(
        chart,
        "gateway",
        metadata=ApiObjectMetadata(
            name=_NAME, namespace=_NAMESPACE, annotations={"cert-manager.io/cluster-issuer": LETSENCRYPT_ISSUER}
        ),
        spec=GatewaySpec(
            gateway_class_name="cilium",
            listeners=[
                Listener.https(
                    name=HTTPS_LISTENER,
                    hostname="*.allegedly.works",
                    port=443,
                    tls=ListenerTls.terminate("wildcard-allegedly-works-tls"),
                    allowed_routes=all_namespaces,
                ),
                Listener.https(
                    name="https-apex",
                    hostname="allegedly.works",
                    port=443,
                    tls=ListenerTls.terminate("apex-allegedly-works-tls"),
                    allowed_routes=all_namespaces,
                ),
                Listener.http(name=_HTTP_LISTENER, port=80, allowed_routes=all_namespaces),
            ],
        ),
    )
    HttpRoute(
        chart,
        "http-redirect",
        metadata=ApiObjectMetadata(name="http-to-https-redirect", namespace=_NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[HttpRouteSpecParentRefs(name=_NAME, section_name=_HTTP_LISTENER)],
            rules=[
                HttpRouteSpecRules(
                    filters=[
                        RouteFilter.request_redirect(
                            scheme=HttpRouteSpecRulesFiltersRequestRedirectScheme.HTTPS,
                            status_code=HttpRouteSpecRulesFiltersRequestRedirectStatusCode.VALUE_301,
                        )
                    ]
                )
            ],
        ),
    )
    return chart


def gateway(
    chart: Chart, directory: RenderedDirectory, cert_manager: Kustomization, kyverno: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart, "gateway", directory, timeout="5m", depends_on=flux_kustomization_depends_on_many(cert_manager, kyverno)
    )
