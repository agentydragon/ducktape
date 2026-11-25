"""The shared CronJob policy and pod scaffold for cluster credential rotators."""

from collections.abc import Sequence

from cdk8s_plus_34 import k8s
from constructs import Construct

from cluster.cdk8s import public_web_pki
from cluster.cdk8s.container_resources import container_resources

_CONFIG_DIR = "/config"
GITHUB_PAT_SECRET_NAME = "github-secrets-sync-pat"


def secret_mount(name: str, secret_name: str, mount_path: str) -> tuple[k8s.Volume, k8s.VolumeMount]:
    """Build a read-only Secret volume and its matching container mount together."""
    return (
        k8s.Volume(name=name, secret=k8s.SecretVolumeSource(secret_name=secret_name)),
        k8s.VolumeMount(name=name, mount_path=mount_path, read_only=True),
    )


class RotatorCronJob(k8s.KubeCronJob):
    """CronJob with the common credential-rotator pod settings and cleanup policy.

    The typed cdk8s_plus_34 CronJob adds a ten-second starting deadline by default. The raw
    construct preserves our manifests' omission of that optional field.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: k8s.ObjectMeta,
        schedule: str,
        image: str,
        config_map_name: str,
        config_file: str,
        service_account_name: str,
        automount_service_account_token: bool,
        image_pull_secrets: list[k8s.LocalObjectReference] | None,
        credential_mounts: Sequence[tuple[k8s.Volume, k8s.VolumeMount]],
    ) -> None:
        pod_spec = k8s.PodSpec(
            service_account_name=service_account_name,
            automount_service_account_token=automount_service_account_token,
            image_pull_secrets=image_pull_secrets,
            restart_policy="OnFailure",
            volumes=[
                public_web_pki.VOLUME,
                k8s.Volume(name="rotations-config", config_map=k8s.ConfigMapVolumeSource(name=config_map_name)),
                k8s.Volume(name="github-pat", secret=k8s.SecretVolumeSource(secret_name=GITHUB_PAT_SECRET_NAME)),
                *(volume for volume, _ in credential_mounts),
            ],
            security_context=k8s.PodSecurityContext(
                run_as_non_root=True, run_as_user=65534, seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")
            ),
            containers=[
                k8s.Container(
                    name="rotate",
                    image=image,
                    args=["--config", f"{_CONFIG_DIR}/{config_file}"],
                    env=[public_web_pki.SSL_CERT_FILE, k8s.EnvVar(name="GIT_SSL_CAINFO", value=public_web_pki.CA_FILE)],
                    security_context=k8s.SecurityContext(
                        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                    ),
                    volume_mounts=[
                        public_web_pki.VOLUME_MOUNT,
                        k8s.VolumeMount(name="rotations-config", mount_path=_CONFIG_DIR, read_only=True),
                        k8s.VolumeMount(name="github-pat", mount_path="/var/run/secrets/github-pat", read_only=True),
                        *(mount for _, mount in credential_mounts),
                    ],
                    resources=container_resources(cpu="100m", memory="128Mi", memory_limit="256Mi"),
                )
            ],
        )
        super().__init__(
            scope,
            id,
            metadata=metadata,
            spec=k8s.CronJobSpec(
                schedule=schedule,
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
                        template=k8s.PodTemplateSpec(spec=pod_spec),
                    )
                ),
            ),
        )
