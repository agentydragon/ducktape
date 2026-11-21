"""The flux-system ExternalSecret supplying credentials to the Route 53 delegation Workspace."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import external_creds
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data

OUTPUT_DIR = f"{GENERATED_ROOT}/dns-automation"
NAME = "dns-automation"
_NAMESPACE = "flux-system"
_CREDENTIALS_SECRET = "aws-route53-credentials"
_CREDENTIALS_SOURCE = "aws-route53-dns-automation-credentials"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Consumer-owned identity for reading approved canonical credentials.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=_NAMESPACE)
    )
    ExternalSecret(
        chart,
        "credentials",
        metadata=ApiObjectMetadata(name=_CREDENTIALS_SECRET, namespace=_NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[
            remote_data(_CREDENTIALS_SOURCE, key)
            for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    return chart
