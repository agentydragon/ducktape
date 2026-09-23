"""The authentik-jwt-rotation CronJob (cluster/k8s/agents/authentik-jwt-rotation), the
agents-infra namespace it owns, the credentials it mounts and the Role letting it read
back what it publishes into flux-system. Source: cluster/rotators/authentik_jwt_rotation.

Hand-written beside the output: `rotations.yaml`, the source of truth for every rotation,
which the directory's `kustomization.yaml` renders into the job's ConfigMap, and
`image-pins/kustomization.yaml`, which overrides the placeholder image tag via Flux's
image-automation marker.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "authentik-jwt-rotation"
OUTPUT_DIR = "cluster/k8s/agents/authentik-jwt-rotation"
NAMESPACE = "agents-infra"
_IMAGE = "git.allegedly.works/ducktape-ci/authentik-jwt-rotation:unset"
# Rendered from rotations.yaml by the hand-written kustomization's configMapGenerator.
_CONFIG_MAP = "authentik-jwt-rotations-config"
_GITHUB_PAT = "github-secrets-sync-pat"
_PUBLISHED_SECRETS_READER = "authentik-jwt-rotation-published-secrets-reader"

# (volume, Secret, mount path) for the Authentik client credentials each rotation exchanges.
_CLIENT_CREDENTIALS = (
    ("claude-web-k8s-creds", "kubectl-sandbox-client-credentials", "/var/run/secrets/authentik/claude-web-k8s"),
    ("haku-k8s-creds", "haku-client-credentials", "/var/run/secrets/authentik/haku-k8s"),
    ("agent-box-codex-creds", "agent-box-codex-client-credentials", "/var/run/secrets/authentik/agent-box-codex"),
    ("haku-mail-creds", "haku-mail-client-credentials", "/var/run/secrets/authentik/haku-mail"),
    ("alloy-otlp-creds", "alloy-otlp-client-credentials", "/var/run/secrets/authentik/alloy-otlp"),
)


def _secret_volume(name: str, secret: str) -> k8s.Volume:
    return k8s.Volume(name=name, secret=k8s.SecretVolumeSource(secret_name=secret))


def _mount(name: str, path: str) -> k8s.VolumeMount:
    return k8s.VolumeMount(name=name, mount_path=path, read_only=True)


def _cronjob(chart: Chart) -> None:
    k8s.KubeCronJob(
        chart,
        "cronjob",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Rotates every Authentik client_credentials JWT listed in rotations.yaml (the"
                    " ConfigMap-mounted source of truth). Runs hourly; rotate.py reads each output's"
                    " unencrypted `expires_unencrypted` field (no decryption needed) and skips entries"
                    " with > rotate_below_hours remaining, so a real mint runs only ~every 44 days per"
                    " token. Entries with a `probe` also verify the published in-cluster Secret's token"
                    " against its real endpoint each run and re-mint on 401/403. Everything minted this"
                    " cycle lands in one commit."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="15 * * * *",
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    # A Job whose pod can never start (e.g. it pins a kustomize-hashed
                    # ConfigMap that a newer generation pruned) stays active forever --
                    # history limits and TTL apply only to finished Jobs. The deadline
                    # fails it; the TTL then garbage-collects it.
                    active_deadline_seconds=1800,
                    ttl_seconds_after_finished=86400,
                    backoff_limit=2,
                    template=k8s.PodTemplateSpec(
                        spec=k8s.PodSpec(
                            # The SA token is used only to read back the published Secrets for
                            # probes (the published-secrets-reader Roles, get on the
                            # rotator-published Secret names -- nothing else).
                            service_account_name=NAME,
                            automount_service_account_token=True,
                            restart_policy="OnFailure",
                            volumes=[
                                k8s.Volume(
                                    name="rotations-config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP)
                                ),
                                _secret_volume("github-pat", _GITHUB_PAT),
                                *(_secret_volume(volume, secret) for volume, secret, _ in _CLIENT_CREDENTIALS),
                            ],
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True,
                                run_as_user=65534,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            containers=[
                                k8s.Container(
                                    name="rotate",
                                    image=_IMAGE,
                                    args=["--config", "/config/rotations.yaml"],
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                                    ),
                                    volume_mounts=[
                                        _mount("rotations-config", "/config"),
                                        _mount("github-pat", "/var/run/secrets/github-pat"),
                                        *(_mount(volume, path) for volume, _, path in _CLIENT_CREDENTIALS),
                                    ],
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("100m"),
                                            "memory": k8s.Quantity.from_string("128Mi"),
                                        },
                                        limits={"memory": k8s.Quantity.from_string("256Mi")},
                                    ),
                                )
                            ],
                        )
                    ),
                )
            ),
        ),
    )


def _published_secrets_reader(chart: Chart) -> None:
    k8s.KubeRole(
        chart,
        "published-secrets-reader-role",
        metadata=k8s.ObjectMeta(
            name=_PUBLISHED_SECRETS_READER,
            namespace="flux-system",
            annotations={
                "description": (
                    "Lets the authentik-jwt-rotation CronJob read back exactly the Secrets its"
                    " k8s_secret outputs publish, so probes can verify the live tokens against"
                    " their real endpoints. resourceNames must stay in sync with the k8s_secret"
                    " names in rotations.yaml."
                )
            },
        ),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                verbs=["get"],
                resource_names=["haku-cloud-kube-token", "haku-mail-token", "alloy-otlp-bearer"],
            )
        ],
    )
    k8s.KubeRoleBinding(
        chart,
        "published-secrets-reader-rolebinding",
        metadata=k8s.ObjectMeta(name=_PUBLISHED_SECRETS_READER, namespace="flux-system"),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_PUBLISHED_SECRETS_READER),
        subjects=[k8s.Subject(kind="ServiceAccount", name=NAME, namespace=NAMESPACE)],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    k8s.KubeNamespace(chart, "namespace", metadata=k8s.ObjectMeta(name=NAMESPACE, labels={"name": NAMESPACE}))
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )
    ExternalSecret(
        chart,
        "github-pat",
        metadata=metadata(_GITHUB_PAT, NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
                name="kubernetes-external-creds-secret-store",
            ),
            target=ExternalSecretSpecTarget(
                name=_GITHUB_PAT,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                template=ExternalSecretSpecTargetTemplate(type="Opaque"),
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key="token",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key="github-agentydragon-2", property="token"),
                )
            ],
        ),
    )
    k8s.KubeServiceAccount(
        chart,
        "serviceaccount",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Identity for the authentik-jwt-rotation CronJob. Exists solely so the"
                    " published-secrets-reader RoleBinding in flux-system can let the job read"
                    " back the Secrets its own k8s_secret outputs publish (probe support)."
                )
            },
        ),
        image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
    )
    _published_secrets_reader(chart)
    _cronjob(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
