"""Platform session host and its cluster-owned credentials and storage."""

from __future__ import annotations

import json

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_imagepolicy_crds.io.fluxcd.toolkit.image import (
    ImagePolicySpecFilterTags,
    ImagePolicySpecImageRepositoryRef,
    ImagePolicySpecPolicy,
    ImagePolicySpecPolicyAlphabetical,
    ImagePolicySpecPolicyAlphabeticalOrder,
)
from flux_imagerepository_crds.io.fluxcd.toolkit.image import ImageRepositorySpecSecretRef
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from prometheus_operator_podmonitor_crds.com.coreos.monitoring import (
    PodMonitorSpecPodMetricsEndpoints,
    PodMonitorSpecSelector,
)

from cluster.cdk8s import namespaces, pod_policy
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.forgejo_registry.pull_credentials import forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.platform_sessions import config
from cluster.cdk8s.providers.flux.image_policy import ImagePolicy
from cluster.cdk8s.providers.flux.image_repository import ImageRepository
from cluster.cdk8s.providers.prometheus_operator.pod_monitor import PodMonitor
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "platform-sessions"
NAMESPACE = NAME
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"
IMAGE = "git.allegedly.works/ducktape-ci/platform-sessions:unset"
SERVICE = ServiceRef(
    name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=NAMESPACE, labels=(("app", NAME),))
)
CONFIG = ConfigMapArgs(
    name="platform-sessions-config",
    namespace=NAMESPACE,
    data={
        "integrations.json": json.dumps(config.INTEGRATIONS, indent=2),
        "bearers.json": json.dumps(config.BEARERS, indent=2),
    },
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


def _deployment(chart: Chart) -> None:
    def config_env(name: str, key: str) -> k8s.EnvVar:
        return k8s.EnvVar(
            name=name,
            value_from=k8s.EnvVarSource(config_map_key_ref=k8s.ConfigMapKeySelector(name=CONFIG.name, key=key)),
        )

    health = k8s.Probe(http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string("http")))
    secret_sources = [
        k8s.VolumeProjection(
            secret=k8s.SecretProjection(
                name=f"platform-sessions-{name}-bearer", items=[k8s.KeyToPath(key="token", path=name)]
            )
        )
        for name in ("owner", "aiquota", "quest", "thrive-scraper", "thrive-read", "chatgpt-read", "blueshield-read")
    ]
    deployment = k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels={"app": NAME}),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={"app": NAME}),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    node_selector={"kubernetes.io/hostname": "wyrm2"},
                    image_pull_secrets=[k8s.LocalObjectReference(name="forgejo-images-creds")],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        fs_group=1000,
                        fs_group_change_policy="OnRootMismatch",
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="service",
                            image=IMAGE,
                            env_from=[k8s.EnvFromSource(secret_ref=k8s.SecretEnvSource(name="platform-sessions-oidc"))],
                            env=[
                                k8s.EnvVar(name="HOME", value="/tmp"),
                                k8s.EnvVar(
                                    name="PLATFORM_SESSIONS_PUBLIC_BASE_URL",
                                    value="https://platform-sessions.allegedly.works",
                                ),
                                config_env("PLATFORM_SESSIONS_INTEGRATION_CONFIG", "integrations.json"),
                                config_env("PLATFORM_SESSIONS_BEARERS", "bearers.json"),
                            ],
                            ports=[
                                k8s.ContainerPort(name="http", container_port=8080),
                                k8s.ContainerPort(name="metrics", container_port=9090),
                            ],
                            readiness_probe=health,
                            liveness_probe=health,
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("1"),
                                    "memory": k8s.Quantity.from_string("1Gi"),
                                },
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                read_only_root_filesystem=False,
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="platform-sessions", mount_path="/data"),
                                k8s.VolumeMount(
                                    name="bearer-secrets", mount_path="/var/run/platform-sessions", read_only=True
                                ),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="platform-sessions",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=NAME),
                        ),
                        k8s.Volume(
                            name="bearer-secrets",
                            projected=k8s.ProjectedVolumeSource(default_mode=0o440, sources=secret_sources),
                        ),
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )
    pod_policy.harden(deployment)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.DISABLED)
    namespaces.namespace(chart, "retired-namespace", name="openai-platform-session", vpa=Vpa.DISABLED)
    _claim(chart, NAME, NAMESPACE)
    _claim(chart, "openai-platform-session", "openai-platform-session")
    forgejo_images_creds_external_secret(chart, "registry-creds", namespace=NAMESPACE)
    repository = ImageRepository(
        chart,
        "image-repository",
        metadata=ApiObjectMetadata(name=NAME, namespace="flux-system"),
        image="git.allegedly.works/ducktape-ci/platform-sessions",
        interval="5m",
        secret_ref=ImageRepositorySpecSecretRef(name="forgejo-images-creds"),
    )
    ImagePolicy(
        chart,
        "image-policy",
        metadata=ApiObjectMetadata(name=repository.name, namespace=repository.metadata.namespace),
        image_repository_ref=ImagePolicySpecImageRepositoryRef(name=repository.name),
        filter_tags=ImagePolicySpecFilterTags(pattern=r"^main-\d{14}-[0-9a-f]{7}$"),
        policy=ImagePolicySpecPolicy(
            alphabetical=ImagePolicySpecPolicyAlphabetical(order=ImagePolicySpecPolicyAlphabeticalOrder.ASC)
        ),
    )
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
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector={"app": NAME}, ports=[SERVICE.port.k8s_service_port()]),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=["platform-sessions.allegedly.works"],
        backend=SERVICE,
    )
    PodMonitor(
        chart,
        "monitor",
        metadata=ApiObjectMetadata(name="platform-sessions-quest", namespace=NAMESPACE),
        selector=PodMonitorSpecSelector(match_labels={"app": NAME}),
        pod_metrics_endpoints=[PodMonitorSpecPodMetricsEndpoints(port="metrics", path="/metrics", interval="1m")],
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
