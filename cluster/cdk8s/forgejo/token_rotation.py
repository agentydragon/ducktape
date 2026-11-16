"""The forgejo-token-rotation CronJob (cluster/generated/agents/forgejo-token-rotation): mints
Forgejo API tokens for agent service accounts. Source: cluster/rotators/forgejo_token_rotation.
Also the credential sources it mounts and `ROTATIONS`, the roster it runs.

`ROTATIONS` is the rotator's own `Config`, rendered into the hash-suffixed ConfigMap the job
mounts. It only names each output: the rotator alone writes the SOPS files and the tea config
Secret manifests. The image tag is the placeholder "unset"; the hand-written `PINS_DIR`
Component, which the kustomization includes across the roots, overrides it at
`kustomize build` time via Flux's image-automation marker (cluster/cdk8s/AGENTS.md § the
`:tag` Setters marker).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s

from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.flux import ConfigMapArgs
from cluster.cdk8s.forgejo import namespace, secret_copy
from cluster.cdk8s.forgejo_registry.pull_credentials import SECRET_NAME
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.reflector import mirror_annotations
from cluster.cdk8s.rotator_cron_job import RotatorCronJob, secret_mount
from cluster.cdk8s.secret_ref import SecretRef
from cluster.rotators.forgejo_token_rotation.config import Config, Rotation, TeaSecretOutput

NAME = "forgejo-token-rotation"
OUTPUT_DIR = f"{GENERATED_ROOT}/agents/forgejo-token-rotation"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/agents/forgejo-token-rotation-image-pins"
_NAMESPACE = "agents-infra"  # owned by authentik-jwt-rotation
_IMAGE = "git.allegedly.works/ducktape-ci/forgejo-token-rotation:unset"
_CONFIG_FILE = "tokens.yaml"


@dataclass(frozen=True)
class _MountedCredential:
    secret: SecretRef
    mount_directory: Path
    volume_name: str


_HAKU_CREDENTIAL = _MountedCredential(
    secret=SecretRef(namespace=namespace.NAMESPACE, name="forgejo-token-mint-haku"),
    mount_directory=Path("/var/run/secrets/forgejo/haku"),
    volume_name="haku-forgejo",
)
_CLAUDE_CREDENTIAL = _MountedCredential(
    secret=SecretRef(namespace=namespace.NAMESPACE, name="forgejo-token-mint-claude"),
    mount_directory=Path("/var/run/secrets/forgejo/claude"),
    volume_name="claude-forgejo",
)


# Forgejo API tokens for agent service accounts. Omitted `repositories` means full-account
# repository access in Forgejo 15's token API; account-level grants still constrain what each
# agent can do.
_ROTATION_CREDENTIALS: tuple[tuple[_MountedCredential, Rotation], ...] = (
    (
        _HAKU_CREDENTIAL,
        Rotation(
            name="haku",
            credentials_dir=_HAKU_CREDENTIAL.mount_directory,
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
    ),
    (
        _CLAUDE_CREDENTIAL,
        Rotation(
            name="claude",
            credentials_dir=_CLAUDE_CREDENTIAL.mount_directory,
            sops_file=Path("secrets/claude-forgejo-tea-token.yaml"),
            token_prefix="forgejo-tea-claude",
            tea_secret=TeaSecretOutput(
                path=Path("cluster/k8s/agents/claude-sandbox-secrets/claude-forgejo-tea.sops.yaml"),
                name="claude-forgejo-tea",
                namespace="claude-sandbox",
            ),
        ),
    ),
)
ROTATIONS = Config(rotations=[rotation for _, rotation in _ROTATION_CREDENTIALS])


def _unique_credentials() -> tuple[_MountedCredential, ...]:
    """Return mounted credentials in rotation order, once per mount directory."""
    credentials: dict[Path, _MountedCredential] = {}
    for credential, _ in _ROTATION_CREDENTIALS:
        previous = credentials.setdefault(credential.mount_directory, credential)
        if previous != credential:
            raise ValueError(f"conflicting Forgejo credentials for {credential.mount_directory}")
    return tuple(credentials.values())


# The content hash in the ConfigMap's name rolls the CronJob's template on a roster change.
CONFIG_MAP = ConfigMapArgs(
    name="forgejo-token-rotations-config", namespace=_NAMESPACE, data={_CONFIG_FILE: yaml_config(ROTATIONS)}
)


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    reader = secret_copy.reader(chart, _NAMESPACE)
    credentials = _unique_credentials()
    for credential in credentials:
        if credential.secret.namespace == namespace.NAMESPACE:
            secret_copy.secret_copy(chart, credential.secret.name, reader=reader)
        elif credential.secret.namespace != _NAMESPACE:
            raise ValueError(f"unsupported Forgejo credential source namespace: {credential.secret.namespace}")
    RotatorCronJob(
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
        schedule="35 * * * *",
        image=_IMAGE,
        config_map_name=CONFIG_MAP.name,
        config_file=_CONFIG_FILE,
        # No Kubernetes API access; all source credentials arrive as mounted Secrets.
        service_account_name="",
        automount_service_account_token=False,
        image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
        credential_mounts=tuple(
            secret_mount(credential.volume_name, credential.secret.name, str(credential.mount_directory))
            for credential in credentials
        ),
    )
    return chart
