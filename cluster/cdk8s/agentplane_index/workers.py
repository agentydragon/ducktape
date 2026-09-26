"""The Agentplane repository index: its Namespace, CNPG database, read token, and one
index worker per indexed repository (ducktape, haku-state).

Hand-written beside the generated output: the directory's `image-pins/` Component. The
image tag here is a placeholder the Component overrides.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecBootstrapInitdb
from cnpg_database_crds.io.cnpg.postgresql import (
    Database,
    DatabaseSpec,
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
    DatabaseSpecExtensions,
    DatabaseSpecExtensionsEnsure,
)
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetTemplate,
)

from agentplane.indexing.main import Settings
from cluster.cdk8s import cnpg, forgejo_images
from cluster.cdk8s.flux import ConfigMapArgs, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret
from util.settings_contract import env_name

NAME = "agentplane-index"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agentplane-index"
_DB_CLUSTER = f"{NAME}-db"
# CNPG owns this Secret (username/password).
_DB_APP_SECRET = f"{_DB_CLUSTER}-app"
_DB_OWNER = "indexer"
_READ_TOKEN = f"{NAME}-read-token"
_IMAGE = "git.allegedly.works/ducktape-ci/agentplane-index:unset"
# The worker listens on its Settings default; nothing passes `--port`.
_PORT = Settings.model_fields["port"].default
_REPOSITORY_MOUNT = "/var/lib/agentplane-index"
# The workers' shared settings, rendered by the kustomization.yaml's configMapGenerator.
_CONFIG_MAP = ConfigMapArgs(
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


def _secret_env(name: str, secret: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=secret, key=key))
    )


def _read_token(chart: Chart) -> None:
    Password(
        chart,
        "read-token-generator",
        metadata=metadata(_READ_TOKEN, NAME),
        spec=PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True),
    )
    ExternalSecret(
        chart,
        "read-token",
        name=_READ_TOKEN,
        namespace=NAME,
        refresh=ExternalSecretSpecRefreshPolicy.CREATED_ONCE,
        data_from=[DataFrom.from_password_generator(_READ_TOKEN)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(type="Opaque", data={"token": "{{ .password }}"}),
    )


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "database-cluster",
        name=_DB_CLUSTER,
        namespace=NAME,
        node_selector={"topology.kubernetes.io/zone": "hil-ovh"},
        storage_class="local-path-ovh-ssd",
        size="20Gi",
        initdb=ClusterSpecBootstrapInitdb(database="ducktape", owner=_DB_OWNER),
    )


def _health_probe(*, period_seconds: int | None = None, failure_threshold: int | None = None) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_string("http")),
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
        metadata=metadata(f"{NAME}-{instance}", NAME),
        spec=DatabaseSpec(
            cluster=DatabaseSpecCluster(name=_DB_CLUSTER),
            name=database,
            owner=_DB_OWNER,
            database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
            extensions=[DatabaseSpecExtensions(name="vector", ensure=DatabaseSpecExtensionsEnsure.PRESENT)],
        ),
    )

    labels = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/instance": instance}
    k8s.KubeServiceAccount(chart, f"{instance}-service-account", metadata=k8s.ObjectMeta(name=instance, namespace=NAME))
    k8s.KubeDeployment(
        chart,
        f"{instance}-deployment",
        metadata=k8s.ObjectMeta(name=instance, namespace=NAME, annotations={"reloader.stakater.com/auto": "true"}),
        spec=k8s.DeploymentSpec(
            replicas=replicas,
            selector=k8s.LabelSelector(match_labels=labels),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
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
                            env_from=[k8s.EnvFromSource(config_map_ref=k8s.ConfigMapEnvSource(name=_CONFIG_MAP.name))],
                            env=[
                                _secret_env("DB_USERNAME", _DB_APP_SECRET, "username"),
                                _secret_env("DB_PASSWORD", _DB_APP_SECRET, "password"),
                                k8s.EnvVar(
                                    name=env_name(Settings, "database_url"),
                                    value=(
                                        "postgresql+asyncpg://$(DB_USERNAME):$(DB_PASSWORD)"
                                        f"@{_DB_CLUSTER}-rw.{NAME}.svc/{database}"
                                    ),
                                ),
                                k8s.EnvVar(name=env_name(Settings, "repository_url"), value=url),
                                k8s.EnvVar(name=env_name(Settings, "branch"), value=branch),
                                *env,
                                _secret_env(env_name(Settings, "read_token"), _READ_TOKEN, "token"),
                            ],
                            ports=[k8s.ContainerPort(name="http", container_port=_PORT)],
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
        metadata=k8s.ObjectMeta(name=instance, namespace=NAME),
        spec=k8s.ServiceSpec(
            selector=labels,
            ports=[k8s.ServicePort(name="http", port=_PORT, target_port=k8s.IntOrString.from_string("http"))],
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeNamespace(
        chart,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=NAME,
            labels={
                "name": NAME,
                "goldilocks.fairwinds.com/enabled": "false",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
            annotations={"description": "Single-repository semantic indexes for ducktape and haku-state."},
        ),
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
        env=(k8s.EnvVar(name=env_name(Settings, "ignore"), value="props/specimens/\n*.gz\n"),),
        # CLEANUP(added 2026-09-26): paused so its continuous /v1/embeddings traffic to
        # ollama.ollama stops evicting the much larger qwen3.8-flash-next-q4 chat model
        # mid-load during agentplane/debug/agentplane_ollama_live_smoke_2026_09_24.md's
        # smoke testing. Remove once that test run is done and restore replicas=1.
        replicas=0,
    )
    _worker(
        chart,
        instance="haku-state",
        database="haku_state",
        url="http://forgejo-http.forgejo:3000/haku/haku-state.git",
        branch="main",
        env=(
            _secret_env(env_name(Settings, "git_username"), "haku-forgejo-git", "username"),
            _secret_env(env_name(Settings, "git_password"), "haku-forgejo-git", "password"),
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            resources=[f"{NAME}.k8s.yaml"], components=["./image-pins"], config_map_generator=[_CONFIG_MAP]
        ),
    )
