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

NAME = "atuin"
NAMESPACE = "atuin"
OUTPUT_DIR = f"{GENERATED_ROOT}/atuin"
# CNPG generates the application credentials in `<cluster>-app`.
DB_APP_SECRET = "atuin-db-app"
_DB_CLUSTER = "atuin-db"
_SERVER = "atuin-server"
_PORT = 8888
_LABELS = {"app.kubernetes.io/name": NAME}


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "database",
        name=_DB_CLUSTER,
        namespace=NAMESPACE,
        node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
        storage_class="local-path-ovh-ssd",
        size="2Gi",
        initdb=cnpg.same_owner_initdb(NAME),
    )


def _http_probe(initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_number(_PORT)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _server(chart: Chart) -> None:
    k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=_SERVER, namespace=NAMESPACE, labels=_LABELS),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image="ghcr.io/atuinsh/atuin:18.22.0",
                            args=["start"],
                            ports=[k8s.ContainerPort(container_port=_PORT, name="http")],
                            env=[
                                k8s.EnvVar(name="ATUIN_HOST", value="0.0.0.0"),
                                k8s.EnvVar(name="ATUIN_PORT", value=str(_PORT)),
                                k8s.EnvVar(name="ATUIN_OPEN_REGISTRATION", value="false"),
                                k8s.EnvVar(
                                    name="ATUIN_DB_URI",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(name=DB_APP_SECRET, key="uri")
                                    ),
                                ),
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
        metadata=k8s.ObjectMeta(name=_SERVER, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=_PORT, target_port=k8s.IntOrString.from_number(_PORT), protocol="TCP")
            ],
            type="ClusterIP",
        ),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=["atuin.allegedly.works"],
        backend=_SERVER,
        port=_PORT,
        hsts=False,
        listener=None,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO, agent_readable=None)
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
