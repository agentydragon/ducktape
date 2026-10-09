"""The Attic Nix binary cache (`cache.allegedly.works`): the server and its Postgres, its
SeaweedFS `PrivateBucket`, the cache bootstrap Job, and the hourly JWT rotation.

cdk8s writes the directory Kustomization and the typed rotator roster. The native
`server.toml`, SOPS Secrets, and `image-pins/kustomization.yaml` remain hand-written beside
them; the image-pins Component supplies the rotator image tag (cluster/cdk8s/AGENTS.md §
image-pins).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s import cnpg, external_creds, namespaces, node_scheduling
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import ConfigMapArgs
from cluster.cdk8s.forgejo import images as forgejo_images
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.rotators.attic_jwt_rotation.config import Config, Token

NAME = "attic"
NAMESPACE = "nix-cache"
HOSTNAME = "cache.allegedly.works"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/nix-cache"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),)),
)
DATABASE = cnpg.PostgresRef.generated(name="attic-db", namespace=NAMESPACE)
_GITHUB_PAT = SecretRef(namespace=NAMESPACE, name="github-secrets-sync-pat")
_ROTATOR = "attic-jwt-rotator"
# Placeholder tag; image-pins/kustomization.yaml sets the real one.
_ROTATOR_IMAGE = "git.allegedly.works/ducktape-ci/attic-jwt-rotation:unset"
_ROTATORS_CONFIG_MAP = "attic-rotators-config"
_SERVER_CONFIG_MAP = "attic-config"

ROTATOR_CONFIG = Config(
    tokens=[
        *[
            Token(
                name=f"{sub} attic reader",
                sops_file=Path(secret_file),
                sub=sub,
                validity="1 year",
                pull=["main", "gaffer"],
            )
            for sub, secret_file in [
                ("wyrm2", "secrets/hosts/wyrm2-attic.yaml"),
                ("rugged", "secrets/hosts/rugged-attic.yaml"),
                ("iguana", "secrets/hosts/iguana-attic.yaml"),
                ("atlas", "secrets/hosts/atlas-attic.yaml"),
                ("gecko", "secrets/hosts/gecko-attic.yaml"),
                ("agent-box", "secrets/hosts/agent-box-attic.yaml"),
                ("claude-web", "secrets/claude-web-attic.yaml"),
                ("haku", "secrets/haku-attic.yaml"),
            ]
        ],
        # CI reads main and gaffer for Nix substituter access. Public is an
        # anonymous-readable bootstrap cache required before CI credentials exist.
        Token(
            name="ducktape CI attic writer (main)",
            sops_file=Path("secrets/ci/attic-main-writer.sops.yaml"),
            sub="ducktape-ci",
            validity="1 year",
            pull=["main", "gaffer"],
            push=["main", "public"],
        ),
        Token(
            name="gaffer CI attic writer",
            sops_file=Path("secrets/ci/attic-gaffer-writer.sops.yaml"),
            sub="gaffer-ci",
            validity="1 year",
            pull=["gaffer"],
            push=["gaffer"],
        ),
    ]
)

SERVER_CONFIG_MAP = ConfigMapArgs(name=_SERVER_CONFIG_MAP, namespace=NAMESPACE, files=["server.toml"])
ROTATORS_CONFIG_MAP = ConfigMapArgs(
    name=_ROTATORS_CONFIG_MAP,
    namespace=NAMESPACE,
    literals=[f"rotators.yaml={yaml_config(ROTATOR_CONFIG.model_dump(mode='json', exclude_unset=True))}"],
)


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "db",
        ref=DATABASE,
        image_name=None,
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh",
        size="2Gi",
        initdb=cnpg.same_owner_initdb("attic"),
        wal_archive=False,
    )


def _storage(scope: Construct) -> s3.PrivateBucket:
    return s3.PrivateBucket(
        scope,
        "storage",
        name=NAME,
        tenant=NAMESPACE,
        adopt_existing=True,
        description="attic's NAR chunks; replicated per volume by the SeaweedFS cluster's defaultReplication.",
    )


def _server(scope: Construct, *, storage: s3.PrivateBucket) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    # Co-located with SeaweedFS and attic-db on OVH kimsufi workers.
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    # Stateless S3-backed service; allow control-plane nodes as overflow capacity.
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION],
                    containers=[
                        k8s.Container(
                            name=NAME,
                            # TODO: Pin to a versioned tag once attic publishes semver releases.
                            image="ghcr.io/zhaofengli/attic:latest",
                            image_pull_policy="Always",
                            security_context=k8s.SecurityContext(
                                run_as_user=1000,
                                run_as_group=1000,
                                run_as_non_root=True,
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            env=[
                                DATABASE.app_secret.key("uri").env_var("ATTIC_SERVER_DATABASE_URL"),
                                SecretRef(namespace=NAMESPACE, name="attic-jwt-token")
                                .key("jwt-token")
                                .env_var("ATTIC_SERVER_TOKEN_HS256_SECRET_BASE64"),
                                storage.access_key.env_var("AWS_ACCESS_KEY_ID"),
                                storage.secret_key.env_var("AWS_SECRET_ACCESS_KEY"),
                            ],
                            args=["-f", "/config/server.toml", "--mode", "monolithic"],
                            ports=[SERVICE.port.k8s_container_port()],
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/config", read_only=True),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                            ],
                            liveness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/", port=k8s.IntOrString.from_string(SERVICE.port.name)
                                ),
                                initial_delay_seconds=10,
                                period_seconds=30,
                                timeout_seconds=5,
                            ),
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/", port=k8s.IntOrString.from_string(SERVICE.port.name)
                                ),
                                initial_delay_seconds=5,
                                period_seconds=10,
                                timeout_seconds=3,
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_SERVER_CONFIG_MAP)),
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(type="ClusterIP", ports=[SERVICE.port.k8s_service_port()], selector=SERVICE.pods.selector),
    )
    https_route(
        scope,
        "route",
        metadata=ApiObjectMetadata(name=NAMESPACE, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=SERVICE,
        hsts=False,
        listener=None,
    )


def _rotation(scope: Construct) -> None:
    k8s.KubeServiceAccount(
        scope, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )
    # The rotator's GitHub PAT, copied from the canonical external-creds source
    # (external_creds.py approves nix-cache as a consumer).
    ExternalSecret(
        scope,
        "github-pat",
        metadata=ApiObjectMetadata(name=_GITHUB_PAT.name, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data("github-agentydragon-2", "token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(type="Opaque"),
    )
    k8s.KubeServiceAccount(
        scope,
        "rotator",
        metadata=k8s.ObjectMeta(
            name=_ROTATOR,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Runs the Attic JWT rotation CronJobs. RBAC is `pods/exec` scoped to the nix-cache namespace so"
                    " the rotator can `kubectl exec deploy/attic -- atticadm make-token …` without ever holding the"
                    " HS256 signing secret."
                )
            },
        ),
        image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
    )
    k8s.KubeRole(
        scope,
        "rotator-role",
        metadata=k8s.ObjectMeta(name=_ROTATOR, namespace=NAMESPACE),
        rules=[
            # `kubectl exec` requires get on pods (to resolve deploy/attic to a Pod name) plus
            # create on pods/exec (to open the exec stream).
            k8s.PolicyRule(api_groups=[""], resources=["pods"], verbs=["get", "list"]),
            k8s.PolicyRule(api_groups=[""], resources=["pods/exec"], verbs=["create"]),
            # Resolving `deploy/attic` to a pod first reads the Deployment.
            k8s.PolicyRule(api_groups=["apps"], resources=["deployments"], verbs=["get"]),
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "rotator-binding",
        metadata=k8s.ObjectMeta(name=_ROTATOR, namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_ROTATOR),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_ROTATOR, namespace=NAMESPACE)],
    )
    k8s.KubeCronJob(
        scope,
        "rotation",
        metadata=k8s.ObjectMeta(
            name="attic-jwt-rotation",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Rotates all Attic JWTs (per-host readers, claude-web reader, ducktape and gaffer CI writer"
                    " tokens) defined in rotators.yaml. Hourly cron; per-token freshness check skips entries with"
                    " >24h remaining, so a real mint runs ~once per token per year. See rotate.py."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="30 * * * *",
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    # A Job whose pod can never start (e.g. it pins a kustomize-hashed ConfigMap
                    # that a newer generation pruned) stays active forever -- history limits and
                    # TTL apply only to finished Jobs. The deadline fails it; the TTL then
                    # garbage-collects it.
                    active_deadline_seconds=1800,
                    ttl_seconds_after_finished=86400,
                    backoff_limit=2,
                    template=k8s.PodTemplateSpec(
                        spec=k8s.PodSpec(
                            service_account_name=_ROTATOR,
                            # The rotator runs `kubectl exec deploy/attic` with this token.
                            automount_service_account_token=True,
                            restart_policy="OnFailure",
                            volumes=[
                                k8s.Volume(
                                    name="rotators-config",
                                    config_map=k8s.ConfigMapVolumeSource(name=_ROTATORS_CONFIG_MAP),
                                )
                            ],
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True,
                                run_as_user=65534,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            containers=[
                                k8s.Container(
                                    name="rotate",
                                    image=_ROTATOR_IMAGE,
                                    args=["rotate", "--config", "/config/rotators.yaml"],
                                    env=[_GITHUB_PAT.key("token").env_var("GIT_TOKEN")],
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                                    ),
                                    volume_mounts=[
                                        k8s.VolumeMount(name="rotators-config", mount_path="/config", read_only=True)
                                    ],
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("100m"),
                                            "memory": k8s.Quantity.from_string("256Mi"),
                                        },
                                        limits={"memory": k8s.Quantity.from_string("512Mi")},
                                    ),
                                )
                            ],
                        )
                    ),
                )
            ),
        ),
    )
    k8s.KubeJob(
        scope,
        "bootstrap-caches",
        metadata=k8s.ObjectMeta(
            name="attic-bootstrap-caches",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Idempotently ensure required Attic caches (main, gaffer, public) exist and print their"
                    " public keys."
                ),
                # Force Flux to recreate the Job when its hash (image or args) changes.
                "kustomize.toolkit.fluxcd.io/force": "enabled",
            },
        ),
        spec=k8s.JobSpec(
            backoff_limit=5,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={"app": "attic-bootstrap-caches"}),
                spec=k8s.PodSpec(
                    # Reuses the rotator's ServiceAccount -- same pods/exec RBAC for invoking
                    # atticadm against deploy/attic. The bootstrap subcommand doesn't touch SOPS
                    # or git, so it doesn't need the rotator's GitHub PAT.
                    service_account_name=_ROTATOR,
                    automount_service_account_token=True,
                    # Keep the registry credential explicit on the Pod. The Secret is reflected
                    # into nix-cache; this avoids relying on ServiceAccount admission timing for
                    # the image pull.
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    restart_policy="OnFailure",
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65534,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="bootstrap",
                            image=_ROTATOR_IMAGE,
                            args=[
                                "bootstrap-caches",
                                "--cache=main",
                                "--cache=gaffer",
                                "--public-cache=public",
                                "--keypair-dir=/secrets/cache-keys",
                            ],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="cache-keys", mount_path="/secrets/cache-keys", read_only=True)
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="cache-keys", secret=k8s.SecretVolumeSource(secret_name="attic-cache-keys"))
                    ],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    _database(chart)
    _server(chart, storage=_storage(chart))
    _rotation(chart)
    return chart
