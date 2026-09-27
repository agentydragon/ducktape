"""Credentials delivered into the claude-sandbox namespace
(cluster/k8s/agents/claude-sandbox-secrets): the external-creds referent ServiceAccount and
the ExternalSecrets copying approved external credentials in. The two SOPS-encrypted Secrets
beside the output stay hand-written.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s import external_creds
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data

NAME = "claude-sandbox-secrets"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/claude-sandbox-secrets"
_NAMESPACE = "claude-sandbox"
SOPS_FILES = ("claude-web-age-key.sops.yaml", "claude-forgejo-tea.sops.yaml")
_TELEGRAM_BOT_TOKEN = "openclaw-telegram-bot-token"
_BUILDBUDDY_API_KEY = "buildbuddy-api-key"


def _external_creds_secret(
    chart: Chart, name: str, *, key: str, deletion_policy: ExternalSecretSpecTargetDeletionPolicy | None
) -> None:
    ExternalSecret(
        chart,
        name,
        metadata=ApiObjectMetadata(name=name, namespace=_NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data(name, key)],
        # The target already existed without an ExternalSecret owner reference. Orphan
        # lets ESO refresh it in place; deleting this ExternalSecret leaves the target
        # Secret behind, which then needs explicit cleanup.
        creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
        deletion_policy=deletion_policy,
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Consumer-owned referent identity for source-approved external credentials.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=_NAMESPACE)
    )
    _external_creds_secret(chart, _TELEGRAM_BOT_TOKEN, key="token", deletion_policy=None)
    _external_creds_secret(
        chart, _BUILDBUDDY_API_KEY, key="api-key", deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN
    )
    return chart


def claude_sandbox_secrets(
    chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="5m", depends_on=flux_kustomization_depends_on_many(external_secrets_operator)
    )
