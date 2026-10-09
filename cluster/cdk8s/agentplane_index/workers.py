"""The Agentplane repository index: its Namespace, CNPG database, read token, one index
worker per indexed repository (ducktape, haku-state), and its Flux Kustomization. Operation:
README.md beside this module.

The image tag is a placeholder; the hand-written `PINS_DIR` Component, which the
kustomization includes across the roots, overrides it via Flux's image-automation marker.
"""

from __future__ import annotations

import textwrap

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecBootstrapInitdb
from cnpg_database_crds.io.cnpg.postgresql import (
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
    DatabaseSpecExtensions,
    DatabaseSpecExtensionsEnsure,
)
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthCheckExprs, KustomizationSpecHealthChecks

from agentplane.indexing.settings import Settings
from cluster.cdk8s import cnpg, namespaces, node_scheduling
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.forgejo import (
    app as forgejo,  # a bare `app.HTTP` would not say whose
    images as forgejo_images,
)
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cnpg.database import Database
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.settings_contract import env_name

NAME = "agentplane-index"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/agentplane-index-image-pins"
DATABASE = cnpg.PostgresRef.generated(name=f"{NAME}-db", namespace=NAME)
_DB_OWNER = "indexer"
_READ_TOKEN = SecretRef(namespace=NAME, name=f"{NAME}-read-token").key("token")
_HAKU_FORGEJO_GIT = SecretRef(namespace=NAME, name="haku-forgejo-git")
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-index:unset"
# The worker listens on its Settings default; nothing passes `--port`.
_HTTP = Port(name="http", number=Settings.model_fields["port"].default)
_REPOSITORY_MOUNT = "/var/lib/agentplane-index"
# The workers' shared settings, rendered by the kustomization.yaml's configMapGenerator.
CONFIG_MAP = ConfigMapArgs(
    name=f"{NAME}-config",
    namespace=NAME,
    literals=[
        f"{env_name(Settings, field)}={value}"
        for field, value in {
            "checkout_dir": f"{_REPOSITORY_MOUNT}/repository",
            # libgit2 does not read SSL_CERT_FILE; the bundle comes from the cacerts image layer.
            "git_ca_bundle": "/etc/ssl/certs/ca-certificates.crt",
            "embedding_url": "http://ollama.ollama.svc.cluster.local:11434/v1",
            "embedding_model": "qwen3-embedding:4b",
            "embedding_api_key": "ollama",
            # kustomize strips a literal's surrounding quotes; these keep the trailing space from
            # the trailing-whitespace hook, which would otherwise trim the rendered block scalar.
            "query_instruction": (
                '"Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "'
            ),
            "embedding_timeout_seconds": "120",
        }.items()
    ],
)


def _service(instance: str) -> ServiceRef:
    """One repository's index worker."""
    return ServiceRef(
        name=instance,
        port=_HTTP,
        pods=Pods(namespace=NAME, labels=(("app.kubernetes.io/name", NAME), ("app.kubernetes.io/instance", instance))),
    )


def _read_token(chart: Chart) -> None:
    mint_bearer_secret(
        chart,
        "read-token",
        name=_READ_TOKEN.secret.name,
        namespace=_READ_TOKEN.secret.namespace,
        key=_READ_TOKEN.key,
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "database-cluster",
        ref=DATABASE,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-ssd",
        size="20Gi",
        initdb=ClusterSpecBootstrapInitdb(database="ducktape", owner=_DB_OWNER),
        wal_archive=False,
    )


def _health_probe(*, period_seconds: int | None = None, failure_threshold: int | None = None) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string(_HTTP.name)),
        timeout_seconds=5,
        period_seconds=period_seconds,
        failure_threshold=failure_threshold,
    )


def _worker(
    chart: Chart, *, instance: str, database: str, url: str, branch: str, env: tuple[k8s.EnvVar, ...], replicas: int = 1
) -> None:
    """One repository's database, and the index worker serving it."""
    Database(
        chart,
        f"{instance}-database",
        metadata=ApiObjectMetadata(name=f"{NAME}-{instance}", namespace=NAME),
        cluster=DatabaseSpecCluster(name=DATABASE.name),
        name=database,
        owner=_DB_OWNER,
        database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
        extensions=[DatabaseSpecExtensions(name="vector", ensure=DatabaseSpecExtensionsEnsure.PRESENT)],
    )

    worker = _service(instance)
    k8s.KubeServiceAccount(chart, f"{instance}-service-account", metadata=k8s.ObjectMeta(name=instance, namespace=NAME))
    k8s.KubeDeployment(
        chart,
        f"{instance}-deployment",
        metadata=k8s.ObjectMeta(name=instance, namespace=NAME),
        spec=k8s.DeploymentSpec(
            replicas=replicas,
            selector=k8s.LabelSelector(match_labels=worker.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=worker.pods.selector),
                spec=k8s.PodSpec(
                    service_account_name=instance,
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    node_selector={"topology.kubernetes.io/region": "hil"},
                    # The worker reads its repository itself; nothing here talks to the API server.
                    automount_service_account_token=False,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=1000,
                        run_as_group=1000,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="index",
                            image=_IMAGE,
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            env_from=[k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name=CONFIG_MAP.name))],
                            env=[
                                DATABASE.app_secret.key("username").env_var("DB_USERNAME"),
                                DATABASE.app_secret.key("password").env_var("DB_PASSWORD"),
                                k8s.EnvVar(
                                    name=env_name(Settings, "database_url"),
                                    value=(
                                        "postgresql+asyncpg://$(DB_USERNAME):$(DB_PASSWORD)"
                                        f"@{DATABASE.rw.host}/{database}"
                                    ),
                                ),
                                k8s.EnvVar(name=env_name(Settings, "repository_url"), value=url),
                                k8s.EnvVar(name=env_name(Settings, "branch"), value=branch),
                                *env,
                                _READ_TOKEN.env_var(env_name(Settings, "read_token")),
                            ],
                            ports=[worker.port.k8s_container_port()],
                            volume_mounts=[k8s.VolumeMount(name="repository", mount_path=_REPOSITORY_MOUNT)],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("250m"),
                                    "memory": k8s.Quantity.from_string("512Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("2Gi")},
                            ),
                            readiness_probe=_health_probe(),
                            liveness_probe=_health_probe(period_seconds=30),
                            startup_probe=_health_probe(period_seconds=5, failure_threshold=60),
                        )
                    ],
                    volumes=[
                        # A bare clone, fetched incrementally; a pod restart re-clones once.
                        k8s.Volume(
                            name="repository",
                            empty_dir=k8s.EmptyDirVolumeSource(size_limit=k8s.Quantity.from_string("4Gi")),
                        )
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        chart,
        f"{instance}-service",
        metadata=k8s.ObjectMeta(name=worker.name, namespace=worker.pods.namespace),
        spec=k8s.ServiceSpec(selector=worker.pods.selector, ports=[worker.port.k8s_service_port()]),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAME,
        vpa=Vpa.DISABLED,
        labels={"name": NAME},
        annotations={"description": "Single-repository semantic indexes for ducktape and haku-state."},
    )
    _read_token(chart)
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAME)
    _database(chart)
    _worker(
        chart,
        instance="ducktape",
        database="ducktape",
        url="https://github.com/agentydragon/ducktape.git",
        branch="devel",
        # gitignore syntax. Specimens duplicate code indexed at its real path; the .gz
        # reference blobs are not text and would only cost the clone read.
        env=(
            k8s.EnvVar(
                name=env_name(Settings, "ignore"),
                value=textwrap.dedent(
                    """\
                    props/specimens/
                    *.gz
                    """
                ),
            ),
        ),
        # CLEANUP(added 2026-09-27): pause both workers while Ollama model setup and API
        # smoke tests run. Their continuous /v1/embeddings traffic evicts the loaded chat
        # model; restore replicas=1 for both workers when indexing resumes.
        replicas=0,
    )
    _worker(
        chart,
        instance="haku-state",
        database="haku_state",
        url=f"{forgejo.HTTP.url}/haku/haku-state.git",
        branch="main",
        env=(
            _HAKU_FORGEJO_GIT.key("username").env_var(env_name(Settings, "git_username")),
            _HAKU_FORGEJO_GIT.key("password").env_var(env_name(Settings, "git_password")),
        ),
        replicas=0,
    )
    return chart


def agentplane_index(
    flux_chart: Chart,
    directory: RenderedDirectory,
    cnpg: Kustomization,
    external_secrets_operator: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        timeout="10m",
        # The haku-state index worker reads haku-forgejo-git, which the haku-state
        # Terraform reflects into this Namespace; waiting for that worker would hold
        # this Kustomization NotReady until the Terraform has applied.
        wait=False,
        health_checks=[
            KustomizationSpecHealthChecks(api_version="v1", kind="Namespace", name=NAME),
            KustomizationSpecHealthChecks(
                api_version="postgresql.cnpg.io/v1", kind="Cluster", name=DATABASE.name, namespace=NAME
            ),
            KustomizationSpecHealthChecks(api_version="apps/v1", kind="Deployment", name="ducktape", namespace=NAME),
        ],
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1",
                kind="Database",
                current=(
                    "has(status.applied) && status.applied && "
                    "has(status.observedGeneration) && status.observedGeneration == "
                    "metadata.generation && has(status.extensions) && "
                    "status.extensions.exists(e, e.name == 'vector' && e.applied)"
                ),
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            cnpg,
            external_secrets_operator,
            # Kyverno's failurePolicy: Fail webhooks admit the Deployments and Namespace.
            kyverno,
        ),
        description=(
            "Complete Agentplane repository-index service: namespace, ESO "
            "credentials, CNPG databases, and both index workers."
        ),
    )
