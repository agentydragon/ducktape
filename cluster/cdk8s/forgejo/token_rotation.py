"""The forgejo-token-rotation CronJob (cluster/generated/agents/forgejo-token-rotation): mints
Forgejo API tokens for agent service accounts. Source: cluster/rotators/forgejo_token_rotation.
Also the copy of the `haku` and `claude` accounts' passwords that it mints their tokens with,
and `ROTATIONS`, the roster it runs.

`ROTATIONS` is the rotator's own `Config`, rendered into the hash-suffixed ConfigMap the job
mounts. It only names each output: the rotator alone writes the SOPS files and the tea config
Secret manifests. The image tag is the placeholder "unset"; the hand-written `PINS_DIR`
Component, which the kustomization includes across the roots, overrides it at
`kustomize build` time via Flux's image-automation marker (cluster/cdk8s/AGENTS.md § the
`:tag` Setters marker).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.forgejo import secret_copy
from cluster.cdk8s.forgejo_registry.chart import SECRET_NAME
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.reflector import mirror_annotations
from cluster.rotators.forgejo_token_rotation.config import Config, Rotation, TeaSecretOutput

NAME = "forgejo-token-rotation"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/forgejo-token-rotation"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/agents/forgejo-token-rotation-image-pins"
_NAMESPACE = "agents-infra"  # owned by authentik-jwt-rotation
_IMAGE = "git.allegedly.works/ducktape-ci/forgejo-token-rotation:unset"
_GITHUB_PAT = "github-secrets-sync-pat"
_CONFIG_DIR = "/config"
_CONFIG_FILE = "tokens.yaml"


# Forgejo API tokens for agent service accounts. Omitted `repositories` means full-account
# repository access in Forgejo 15's token API; account-level grants still constrain what each
# agent can do.
ROTATIONS = Config(
    rotations=[
        Rotation(
            name="haku",
            credentials_dir=Path("/var/run/secrets/forgejo/haku"),
            sops_file=Path("secrets/haku-forgejo-tea-token.yaml"),
            token_prefix="forgejo-tea-haku",
            tea_secret=TeaSecretOutput(
                path=Path("cluster/k8s/haku/forgejo-tea/haku-forgejo-tea.sops.yaml"),
                name="haku-forgejo-tea",
                namespace="haku-sandbox",
                # KEDA is the only extra consumer. Reflector creates the destination Secret, and
                # the haku-ci runner itself never mounts it.
                annotations=mirror_annotations(["haku-ci"]),
            ),
        ),
        Rotation(
            name="claude",
            credentials_dir=Path("/var/run/secrets/forgejo/claude"),
            sops_file=Path("secrets/claude-forgejo-tea-token.yaml"),
            token_prefix="forgejo-tea-claude",
            tea_secret=TeaSecretOutput(
                path=Path("cluster/k8s/agents/claude-sandbox-secrets/claude-forgejo-tea.sops.yaml"),
                name="claude-forgejo-tea",
                namespace="claude-sandbox",
            ),
        ),
        Rotation(
            name="agent-box-codex",
            credentials_dir=Path("/var/run/secrets/forgejo/agent-box-codex"),
            sops_file=Path("secrets/agent-box-codex-forgejo-tea-token.yaml"),
            token_prefix="forgejo-tea-agent-box-codex",
        ),
    ]
)

# These two source Secrets live in the forgejo namespace and need ESO copies into
# agents-infra. The agent-box-codex source is already created in agents-infra by
# tf/gitops/forgejo-agentydragon-repos. Rotation has no Secret namespace field because
# this is deployment wiring, not input read by the rotator; keep only that namespace
# exception here, keyed by credentials_dir.name, and derive copied Secret names from the
# model entries below.
_FORGEJO_NAMESPACE_CREDENTIALS = frozenset({"haku", "claude"})


def _unique_credentials(rotations: list[Rotation]) -> tuple[Rotation, ...]:
    """One rotation per mounted credential directory, retaining first-use order."""
    credentials: dict[Path, Rotation] = {}
    for rotation in rotations:
        credentials.setdefault(rotation.credentials_dir, rotation)
    return tuple(credentials.values())


def _credentials_secret(rotation: Rotation) -> str:
    return f"forgejo-token-mint-{rotation.credentials_dir.name}"


def _credentials_volume(rotation: Rotation) -> str:
    return f"{rotation.credentials_dir.name}-forgejo"


# The content hash in the ConfigMap's name rolls the CronJob's template on a roster change.
CONFIG_MAP = ConfigMapArgs(
    name="forgejo-token-rotations-config",
    namespace=_NAMESPACE,
    literals=[f"{_CONFIG_FILE}={yaml_config(ROTATIONS.model_dump(mode='json', exclude_unset=True))}"],
)


def _secret_volume(name: str, secret: str) -> k8s.Volume:
    return k8s.Volume(name=name, secret=k8s.SecretVolumeSource(secret_name=secret))


def _mount(name: str, path: str) -> k8s.VolumeMount:
    return k8s.VolumeMount(name=name, mount_path=path, read_only=True)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    reader = secret_copy.reader(chart, _NAMESPACE)
    credentials = _unique_credentials(ROTATIONS.rotations)
    for rotation in credentials:
        if rotation.credentials_dir.name in _FORGEJO_NAMESPACE_CREDENTIALS:
            secret_copy.secret_copy(chart, _credentials_secret(rotation), reader=reader)
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
                                    name="rotations-config", config_map=k8s.ConfigMapVolumeSource(name=CONFIG_MAP.name)
                                ),
                                _secret_volume("github-pat", _GITHUB_PAT),
                                *(
                                    _secret_volume(_credentials_volume(rotation), _credentials_secret(rotation))
                                    for rotation in credentials
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
                                    args=["--config", f"{_CONFIG_DIR}/{_CONFIG_FILE}"],
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                                    ),
                                    volume_mounts=[
                                        _mount("rotations-config", _CONFIG_DIR),
                                        _mount("github-pat", "/var/run/secrets/github-pat"),
                                        *(
                                            _mount(_credentials_volume(rotation), str(rotation.credentials_dir))
                                            for rotation in credentials
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


def forgejo_token_rotation(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        retry_interval=None,
        wait=None,
        timeout="2m",
        depends_on=flux_kustomization_depends_on_many(external_secrets_operator),
    )
