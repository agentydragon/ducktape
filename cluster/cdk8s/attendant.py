"""A single owner-operated Chromium desktop and its durable profile."""

from cdk8s import ApiObjectMetadata, App, Chart, Duration, Size
from cdk8s_plus_34 import (
    ContainerPort,
    ContainerResources,
    Cpu,
    CpuResources,
    Deployment,
    DeploymentStrategy,
    EnvValue,
    MemoryResources,
    Node,
    NodeLabelQuery,
    PersistentVolumeAccessMode,
    PersistentVolumeClaim,
    PodSecurityContextProps,
    Probe,
    Service,
    ServicePort,
    Volume,
    k8s,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import gateway, namespaces, node_scheduling, piraeus_storage, pod_policy
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry import image_automation, pull_credentials
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "attendant"
HOST = "attendant.allegedly.works"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/{NAME}-image-pins"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAME, vpa=namespaces.Vpa.RECOMMEND)
    pull_credentials.forgejo_images_creds_external_secret(chart, "pull-credentials", namespace=NAME)
    profile = PersistentVolumeClaim(
        chart,
        "profile",
        metadata=ApiObjectMetadata(
            name="profile", namespace=NAME, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
        storage_class_name=piraeus_storage.SSD_REPLICATED,
        storage=Size.gibibytes(2),
    )
    env = {"HOME": EnvValue.from_value("/tmp")}
    for role in ("owner", "blueshield", "myquest", "quest-scheduler"):
        token = SecretRef(namespace=NAME, name=role).key("token")
        mint_bearer_secret(chart, f"{role}-token", target=token, immutable=True)
        env[f"ATTENDANT_{role.upper().replace('-', '_')}_TOKEN"] = token.env_value(chart, f"{role}-ref")

    oidc = SecretRef(namespace=NAME, name="attendant-oidc")
    for key in ("ISSUER", "CLIENT_ID", "CLIENT_SECRET", "SESSION_SECRET", "ALLOWED_SUBJECT"):
        var = f"ATTENDANT_{key}"
        env[var] = oidc.key(var).env_value(chart, f"oidc-{key.lower().replace('_', '-')}")

    metadata = ApiObjectMetadata(name=NAME, namespace=NAME)
    deployment = Deployment(
        chart,
        "desktop",
        metadata=metadata,
        replicas=1,
        strategy=DeploymentStrategy.recreate(),
        docker_registry_auth=pull_credentials.forgejo_images_creds_secret_ref(chart, "pull-secret"),
        security_context=PodSecurityContextProps(ensure_non_root=True, user=1000, group=1000, fs_group=1000),
    )
    deployment.scheduling.attract(
        Node.labeled(NodeLabelQuery.in_(node_scheduling.ZONE_LABEL, [node_scheduling.HIL_OVH_ZONE]))
    )
    owner_pods = Pods(namespace=NAME, labels=(("attendant.allegedly.works/owner-auth", "oidc"),))
    for key, value in owner_pods.labels:
        deployment.pod_metadata.add_label(key, value)
    port = 8080
    health = Probe.from_http_get("/healthz", port=port)
    desktop = deployment.add_container(
        name=NAME,
        image=f"{image_automation.REGISTRY}/{NAME}:unset",
        args=[
            "--state-dir",
            "/data/attendant",
            "--host",
            "0.0.0.0",
            "--public-origin",
            f"https://{HOST}",
            "--owner-oidc",
        ],
        env_variables=env,
        ports=[ContainerPort(name="http", number=port)],
        readiness=health,
        liveness=health,
        startup=Probe.from_http_get("/healthz", port=port, period_seconds=Duration.seconds(5), failure_threshold=24),
        resources=ContainerResources(
            cpu=CpuResources(request=Cpu.millis(100), limit=Cpu.units(2)),
            memory=MemoryResources(request=Size.mebibytes(512), limit=Size.gibibytes(2)),
        ),
    )
    desktop.mount("/data", Volume.from_persistent_volume_claim(chart, "data", profile))
    desktop.mount("/tmp", Volume.from_empty_dir(chart, "tmp", "tmp"))
    pod_policy.harden(deployment)
    Service(chart, "service", metadata=metadata, selector=deployment, ports=[ServicePort(name="http", port=port)])
    owner = ServiceRef(name="owner", pods=owner_pods, port=Port(name="http", number=port))
    k8s.KubeService(
        chart,
        "owner-service",
        metadata=k8s.ObjectMeta(name=owner.name, namespace=NAME),
        spec=k8s.ServiceSpec(selector=owner.pods.selector, ports=[owner.port.k8s_service_port()]),
    )
    gateway.https_route(
        chart,
        "owner-route",
        metadata=metadata,
        hostnames=[HOST],
        backend=owner,
        paths=["/", "/main.js", "/main.css", "/auth/login", "/auth/callback", "/auth/logout", "/auth/signed-out"],
        path_prefix="/owner",
    )
    return chart


def attendant(
    chart: Chart,
    directory: RenderedDirectory,
    secret_stores: Kustomization,
    registry: Kustomization,
    storage: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        description="Owner-operated Chrome desktop with a persistent profile.",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(secret_stores, registry, storage),
    )
