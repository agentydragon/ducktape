"""Route 53 domain delegation and the flux-system credential its Terraform runner reads."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from pydantic import BaseModel, ConfigDict
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import external_creds, terraform
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data

OUTPUT_DIR = f"{GENERATED_ROOT}/dns-automation"
TF_MODULE = "dns-records"
_NAMESPACE = "flux-system"
_CREDENTIALS_SECRET = "aws-route53-credentials"
_CREDENTIALS_SOURCE = "aws-route53-dns-automation-credentials"


class DnsRecordsVars(BaseModel):
    """The inputs of tf/gitops/dns-records; `aws_region` keeps its variables.tf default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route53_zone_id: str


def chart(app: App, module: ArtifactGeneratorSpecArtifacts) -> Chart:
    """Route 53 domain delegation for allegedly.works (tf/gitops/dns-records)."""
    chart = Chart(app, TF_MODULE, disable_resource_name_hashes=True)
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
    terraform.gitops_terraform(
        chart,
        "terraform",
        module=module,
        variables=DnsRecordsVars(route53_zone_id="Z02901943N8ZFQFOD9P5I"),
        interval="2h",
        env_from=[terraform.secret_env_from(_CREDENTIALS_SECRET)],
    )
    return chart


def dns_automation(
    chart: Chart, directory: RenderedDirectory, tofu_controller: Kustomization, external_secrets_operator: Kustomization
) -> Kustomization:
    return flux_kustomization(
        chart,
        "dns-automation",
        directory,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="infra.contrib.fluxcd.io/v1alpha2",
                kind="Terraform",
                name=TF_MODULE,
                namespace="flux-system",
            )
        ],
        depends_on=flux_kustomization_depends_on_many(tofu_controller, external_secrets_operator),
    )
