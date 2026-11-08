"""Platform Sessions public entry point and shared credentials and storage."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import namespaces
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry.pull_credentials import forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.platform_sessions import remote
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "platform-sessions"
NAMESPACE = NAME
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
SERVICE = ServiceRef(
    name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=NAMESPACE, labels=(("app", remote.GATEWAY),))
)


def _claim(chart: Chart, name: str, namespace: str) -> None:
    k8s.KubePersistentVolumeClaim(
        chart,
        f"claim-{namespace}",
        metadata=k8s.ObjectMeta(name=name, namespace=namespace),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="piraeus-ssd-local",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.DISABLED)
    namespaces.namespace(chart, "retired-namespace", name="openai-platform-session", vpa=Vpa.DISABLED)
    _claim(chart, NAME, NAMESPACE)
    _claim(chart, "openai-platform-session", "openai-platform-session")
    forgejo_images_creds_external_secret(chart, "registry-creds", namespace=NAMESPACE)
    for name, target in (
        ("owner", None),
        ("chatgpt-read", None),
        ("blueshield-read", None),
        ("aiquota", "cli-proxy-api"),
        ("quest", "agentplane-staging-egress-credentials"),
        ("thrive-scraper", "thrive-scraper"),
        ("thrive-read", "agentplane-staging-egress-credentials"),
    ):
        annotations = (
            None
            if target is None
            else {
                "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": target,
                "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": target,
            }
        )
        mint_bearer_secret(
            chart,
            f"{name}-bearer",
            name=f"platform-sessions-{name}-bearer",
            namespace=NAMESPACE,
            key="token",
            creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
            deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
            immutable=True,
            target_annotations=annotations,
        )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector={"app": remote.GATEWAY}, ports=[SERVICE.port.k8s_service_port()]),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=["platform-sessions.allegedly.works"],
        backend=SERVICE,
    )
    return chart


def platform_sessions(
    flux_chart: Chart,
    directory: RenderedDirectory,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
    flux_image_automation: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        description="Authenticated upstream sessions and scoped proxies.",
        suspend=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(
            external_secrets_operator, monitoring_crds, flux_image_automation
        ),
    )
