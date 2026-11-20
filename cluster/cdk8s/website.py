"""The homepage (website/, rendered by Hakyll into the image) on the shared Gateway."""

from __future__ import annotations

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, JsonPatch, Size
from cdk8s_plus_34 import (
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    ImagePullPolicy,
    LabelSelector,
    MemoryResources,
    Pods as PodSelector,
    Service,
)
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersRequestRedirectPath,
    HttpRouteSpecRulesFiltersRequestRedirectPathType,
    HttpRouteSpecRulesFiltersRequestRedirectScheme,
    HttpRouteSpecRulesFiltersRequestRedirectStatusCode,
)

from cluster.cdk8s import disruption_budget, namespaces, node_scheduling, pod_policy, probes
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.pull_credentials import (
    forgejo_images_creds_external_secret,
    forgejo_images_creds_secret_ref,
)
from cluster.cdk8s.gateway import cluster_gateway_parent_ref, https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.gateway_api.http_route import RouteFilter, RouteMatch
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{GENERATED_ROOT}/website"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/website-image-pins"
_NAME = "website"
_NAMESPACE = "website"
HOSTNAME = "www.allegedly.works"
_CANONICAL_HOSTNAME = "www.agentydragon.com"
_MOVED_PERMANENTLY = HttpRouteSpecRulesFiltersRequestRedirectStatusCode.VALUE_301
_FOUND = HttpRouteSpecRulesFiltersRequestRedirectStatusCode.VALUE_302
# Short links printed on stickers and an NFC armband: these paths must keep resolving.
# path -> (redirect hostname, redirect path)
_SHORT_LINKS = {
    "/found": (_CANONICAL_HOSTNAME, "/found.html"),
    "/nfc": (_CANONICAL_HOSTNAME, "/nfc.html"),
    "/furry-rationalists-telegram": ("furryrationalists.proofgrid.tech", "/"),
    "/effective-anthropomorphism-discord": ("discord.com", "/invite/Mcdsh757Ht"),
}
_SERVICE = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=80),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
    target_port=8080,
)

# The PINS_DIR Component sets the real tag.
_IMAGE = "git.allegedly.works/ducktape-ci/website:unset"


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=_NAMESPACE, vpa=Vpa.AUTO)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    deployment = Deployment(
        chart,
        "deployment",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE, labels=_SERVICE.pods.selector),
        pod_metadata=ApiObjectMetadata(labels=_SERVICE.pods.selector),
        replicas=2,
        select=False,
        enable_service_links=True,
        docker_registry_auth=forgejo_images_creds_secret_ref(chart, "forgejo-images-creds-ref"),
    )
    # Deployment selectors are immutable, so keep the existing labels.
    deployment.select(LabelSelector.of(labels=_SERVICE.pods.selector))
    # Each revision spreads independently, so old Pods do not block a two-node rollout.
    ApiObject.of(deployment).add_json_patch(
        JsonPatch.add(
            "/spec/template/spec/topologySpreadConstraints", [node_scheduling.hostname_spread(_SERVICE.pods.selector)]
        )
    )
    deployment.add_container(
        name="server",
        image=_IMAGE,
        image_pull_policy=ImagePullPolicy.IF_NOT_PRESENT,
        ports=[_SERVICE.container_port()],
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(10), limit=Cpu.millis(100)),
            memory=MemoryResources(request=Size.mebibytes(16), limit=Size.mebibytes(64)),
        ),
        readiness=probes.http_probe("/health", port=_SERVICE.pod_port, initial_delay_seconds=2, period_seconds=5),
        liveness=probes.http_probe("/health", port=_SERVICE.pod_port, initial_delay_seconds=5, period_seconds=10),
    )
    pod_policy.harden(deployment)
    disruption_budget.add_disruption_budget(chart, "disruption-budget", name=_NAME, pods=_SERVICE.pods)
    Service(
        chart,
        "service",
        metadata=ApiObjectMetadata(name=_SERVICE.name, namespace=_NAMESPACE),
        selector=PodSelector.select(chart, "service-pods", labels=_SERVICE.pods.selector),
        ports=[_SERVICE.service_port()],
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=[HOSTNAME, "allegedly.works", _CANONICAL_HOSTNAME],
        backend=_SERVICE,
        hsts=False,
        listener=None,
    )
    HttpRoute(
        chart,
        "apex-redirect",
        metadata=ApiObjectMetadata(name="apex-redirect", namespace=_NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["agentydragon.com"],
            rules=[HttpRouteSpecRules(filters=[_redirect(_CANONICAL_HOSTNAME, None, _MOVED_PERMANENTLY)])],
        ),
    )
    HttpRoute(
        chart,
        "short-links",
        metadata=ApiObjectMetadata(name="short-links", namespace=_NAMESPACE),
        spec=HttpRouteSpec(
            parent_refs=[cluster_gateway_parent_ref()],
            hostnames=["adgn.link", "www.adgn.link"],
            rules=[
                HttpRouteSpecRules(filters=[_redirect(_CANONICAL_HOSTNAME, "/", _MOVED_PERMANENTLY)]),
                *(
                    HttpRouteSpecRules(
                        matches=[RouteMatch.path_prefix(path)], filters=[_redirect(hostname, target, _FOUND)]
                    )
                    for path, (hostname, target) in _SHORT_LINKS.items()
                ),
            ],
        ),
    )
    return chart


def _redirect(
    hostname: str, path: str | None, status_code: HttpRouteSpecRulesFiltersRequestRedirectStatusCode
) -> HttpRouteSpecRulesFilters:
    """An HTTPS redirect to `hostname`, at `path` or, when it is None, the request's own path."""
    return RouteFilter.request_redirect(
        scheme=HttpRouteSpecRulesFiltersRequestRedirectScheme.HTTPS,
        hostname=hostname,
        status_code=status_code,
        path=None
        if path is None
        else HttpRouteSpecRulesFiltersRequestRedirectPath(
            type=HttpRouteSpecRulesFiltersRequestRedirectPathType.REPLACE_FULL_PATH, replace_full_path=path
        ),
    )


def website(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization, kyverno: Kustomization
) -> Kustomization:
    name = "website"
    return flux_kustomization(
        chart,
        name,
        directory,
        timeout="5m",
        # Kyverno's failurePolicy: Fail webhooks admit the Deployment and HTTPRoute.
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator, kyverno),
    )
