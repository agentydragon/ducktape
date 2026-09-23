"""The forgejo-token-rotation CronJob (cluster/k8s/agents/forgejo-token-rotation): mints
Forgejo API tokens for agent service accounts. Source: cluster/rotators/.

Hand-written beside the output: `tokens.yaml`, which the directory's `kustomization.yaml`
renders into the ConfigMap the job mounts, and `image-pins/kustomization.yaml`, which
overrides the placeholder image tag via Flux's image-automation marker.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.forgejo_images import SECRET_NAME
from cluster.cdk8s.generation import write_charts

NAME = "forgejo-token-rotation"
OUTPUT_DIR = "cluster/k8s/agents/forgejo-token-rotation"
_NAMESPACE = "agents-infra"  # owned by authentik-jwt-rotation
_IMAGE = "git.allegedly.works/ducktape-ci/forgejo-token-rotation:unset"
# Rendered from tokens.yaml by the hand-written kustomization's configMapGenerator.
_CONFIG_MAP = "forgejo-token-rotations-config"

# (volume, Secret, mount path) for every credential the rotator reads.
_SECRET_MOUNTS = (
    ("github-pat", "github-secrets-sync-pat", "/var/run/secrets/github-pat"),
    ("haku-forgejo", "forgejo-token-mint-haku", "/var/run/secrets/forgejo/haku"),
    ("claude-forgejo", "forgejo-token-mint-claude", "/var/run/secrets/forgejo/claude"),
    ("agent-box-codex-forgejo", "forgejo-token-mint-agent-box-codex", "/var/run/secrets/forgejo/agent-box-codex"),
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    k8s.KubeCronJob(
        chart,
        "cronjob",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Mints full-account Forgejo API tokens for agent service accounts, commits"
                    " SOPS-encrypted raw-token files plus pod-ready tea config Secrets, then prunes older"
                    " rotator-created tokens after the Git push succeeds."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="35 * * * *",
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
                            # No Kubernetes API access; all source credentials arrive as mounted Secrets.
                            service_account_name="",
                            automount_service_account_token=False,
                            image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                            restart_policy="OnFailure",
                            volumes=[
                                k8s.Volume(
                                    name="rotations-config", config_map=k8s.ConfigMapVolumeSource(name=_CONFIG_MAP)
                                ),
                                *(
                                    k8s.Volume(name=volume, secret=k8s.SecretVolumeSource(secret_name=secret))
                                    for volume, secret, _ in _SECRET_MOUNTS
                                ),
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
                                    args=["--config", "/config/tokens.yaml"],
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                                    ),
                                    volume_mounts=[
                                        k8s.VolumeMount(name="rotations-config", mount_path="/config", read_only=True),
                                        *(
                                            k8s.VolumeMount(name=volume, mount_path=path, read_only=True)
                                            for volume, _, path in _SECRET_MOUNTS
                                        ),
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
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
