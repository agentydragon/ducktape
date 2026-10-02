"""The Atuin shell-history sync server: its Namespace, CNPG database, Deployment, Service
and HTTPRoute, all owned by the `atuin` Kustomization."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import cnpg, namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

NAME = "atuin"
NAMESPACE = "atuin"
OUTPUT_DIR = f"{GENERATED_ROOT}/atuin"
DATABASE = cnpg.PostgresRef.generated(name="atuin-db", namespace=NAMESPACE)
SERVER = ServiceRef(
    name="server",
    port=Port(name="http", number=8888),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "database",
        ref=DATABASE,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-ssd",
        size="2Gi",
        initdb=cnpg.same_owner_initdb(NAME),
        wal_archive=False,
    )


def _http_probe(initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_number(SERVER.pod_port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _server(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=SERVER.name, namespace=NAMESPACE, labels=SERVER.pods.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=SERVER.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVER.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image="ghcr.io/atuinsh/atuin:18.22.0",
                            args=["start"],
                            ports=[SERVER.port.k8s_container_port()],
                            env=[
                                k8s.EnvVar(name="ATUIN_HOST", value="0.0.0.0"),
                                k8s.EnvVar(name="ATUIN_PORT", value=str(SERVER.pod_port)),
                                k8s.EnvVar(name="ATUIN_OPEN_REGISTRATION", value="false"),
                                DATABASE.app_secret.key("uri").env_var("ATUIN_DB_URI"),
                                k8s.EnvVar(name="RUST_LOG", value="info"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("500m"),
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                },
                            ),
                            liveness_probe=_http_probe(initial_delay_seconds=10, period_seconds=10),
                            readiness_probe=_http_probe(initial_delay_seconds=5, period_seconds=5),
                        )
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=SERVER.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector=SERVER.pods.selector, ports=[SERVER.port.k8s_service_port()], type="ClusterIP"),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=["atuin.allegedly.works"],
        backend=SERVER,
        hsts=False,
        listener=None,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)
    _database(chart)
    _server(chart)
    return chart


def atuin(chart: Chart, directory: RenderedDirectory, cnpg: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(cnpg),
    )
