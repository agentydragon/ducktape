"""The cluster's shared Gateway API `Gateway` -- every generated `HttpRoute` in this
repo attaches to it -- with its `gateway-system` Namespace, the plaintext listener's
redirect to HTTPS, and the directory's Flux Kustomization.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecPostBuild,
    KustomizationSpecPostBuildSubstituteFrom,
    KustomizationSpecPostBuildSubstituteFromKind,
)
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecParentRefs,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersRequestRedirect,
    HttpRouteSpecRulesFiltersRequestRedirectScheme,
    HttpRouteSpecRulesFiltersRequestRedirectStatusCode,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
    HttpRouteSpecRulesMatches,
    HttpRouteSpecRulesMatchesPath,
    HttpRouteSpecRulesMatchesPathType,
    HttpRouteSpecRulesTimeouts,
)
from gateway_api_gateway_crds.io.k8s.networking.gateway import (
    Gateway,
    GatewaySpec,
    GatewaySpecListeners,
    GatewaySpecListenersAllowedRoutes,
    GatewaySpecListenersAllowedRoutesNamespaces,
    GatewaySpecListenersAllowedRoutesNamespacesFrom,
    GatewaySpecListenersTls,
    GatewaySpecListenersTlsCertificateRefs,
    GatewaySpecListenersTlsMode,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

_NAME = "cluster-gateway"
_NAMESPACE = "gateway-system"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/gateway"
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
        metadata=metadata(_NAME, _NAMESPACE, annotations={"cert-manager.io/cluster-issuer": "${LETSENCRYPT_ISSUER}"}),
        spec=GatewaySpec(
            gateway_class_name="cilium",
            listeners=[
                GatewaySpecListeners(
                    name=HTTPS_LISTENER,
                    hostname="*.allegedly.works",
                    port=443,
                    protocol="HTTPS",
                    tls=GatewaySpecListenersTls(
                        mode=GatewaySpecListenersTlsMode.TERMINATE,
                        certificate_refs=[GatewaySpecListenersTlsCertificateRefs(name="wildcard-allegedly-works-tls")],
                    ),
                    allowed_routes=all_namespaces,
                ),
                GatewaySpecListeners(
                    name="https-apex",
                    hostname="allegedly.works",
                    port=443,
                    protocol="HTTPS",
                    tls=GatewaySpecListenersTls(
                        mode=GatewaySpecListenersTlsMode.TERMINATE,
                        certificate_refs=[GatewaySpecListenersTlsCertificateRefs(name="apex-allegedly-works-tls")],
                    ),
                    allowed_routes=all_namespaces,
                ),
                GatewaySpecListeners(name=_HTTP_LISTENER, port=80, protocol="HTTP", allowed_routes=all_namespaces),
            ],
        ),
    )
    HttpRoute(
        chart,
        "http-redirect",
        metadata=metadata("http-to-https-redirect", _NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[HttpRouteSpecParentRefs(name=_NAME, section_name=_HTTP_LISTENER)],
            rules=[
                HttpRouteSpecRules(
                    filters=[
                        HttpRouteSpecRulesFilters(
                            type=HttpRouteSpecRulesFiltersType.REQUEST_REDIRECT,
                            request_redirect=HttpRouteSpecRulesFiltersRequestRedirect(
                                scheme=HttpRouteSpecRulesFiltersRequestRedirectScheme.HTTPS,
                                status_code=HttpRouteSpecRulesFiltersRequestRedirectStatusCode.VALUE_301,
                            ),
                        )
                    ]
                )
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def gateway(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cert_manager: Kustomization,
    kyverno: Kustomization,
    cert_manager_issuer_config: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "gateway",
        artifact,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(cert_manager, kyverno, cert_manager_issuer_config),
        post_build=KustomizationSpecPostBuild(
            substitute_from=[
                KustomizationSpecPostBuildSubstituteFrom(
                    kind=KustomizationSpecPostBuildSubstituteFromKind.CONFIG_MAP, name="cert-manager-issuer-config"
                )
            ]
        ),
    )
