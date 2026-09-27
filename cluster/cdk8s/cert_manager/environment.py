"""cert-manager's environment: the Route 53 credentials its ACME DNS-01 solvers read,
copied from external-creds, rendered beside the Let's Encrypt ClusterIssuers (`config`) and
the cluster CA (`cluster_ca`)."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import external_creds
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data

NAME = "cert-manager-environment"
NAMESPACE = "cert-manager"
OUTPUT_DIR = f"{GENERATED_ROOT}/cert-manager/environment"
_ROUTE53_SECRET = "aws-route53-credentials"
_ROUTE53_SOURCE = "aws-route53-cert-manager-credentials"


def chart(app: App) -> Chart:
    chart = Chart(app, "environment", disable_resource_name_hashes=True)
    # Consumer-owned identity for reading approved canonical credentials.
    k8s.KubeServiceAccount(chart, "reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE))
    ExternalSecret(
        chart,
        "route53-credentials",
        metadata=ApiObjectMetadata(name=_ROUTE53_SECRET, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[
            remote_data(_ROUTE53_SOURCE, key) for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    return chart


def cert_manager_environment(
    chart: Chart,
    directory: RenderedDirectory,
    cert_manager: Kustomization,
    cert_manager_trust: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="5m",
        depends_on=flux_kustomization_depends_on_many(cert_manager, cert_manager_trust, external_secrets_operator),
    )
