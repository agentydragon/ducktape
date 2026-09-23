"""Route 53 records for allegedly.works (tf/gitops/dns-records), and the flux-system
copy of the Route 53 credential its Terraform runner reads."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecHealthChecks
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import external_creds, terraform
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, remote_data
from cluster.cdk8s.flux import Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.generation import write_charts
from cluster.scripts import nebula_mesh

OUTPUT_DIR = "cluster/k8s/dns-automation"
_NAMESPACE = "flux-system"
_CREDENTIALS_SECRET = "aws-route53-credentials"
_CREDENTIALS_SOURCE = "aws-route53-dns-automation-credentials"


def chart(app: App, mesh: nebula_mesh.Mesh) -> Chart:
    """Route 53 records for allegedly.works (tf/gitops/dns-records)."""
    chart = Chart(app, "dns-records", disable_resource_name_hashes=True)
    # Consumer-owned identity for reading approved canonical credentials.
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=_NAMESPACE)
    )
    add_external_secret(
        chart,
        "credentials",
        name=_CREDENTIALS_SECRET,
        namespace=_NAMESPACE,
        refresh="1h",
        store=external_creds.STORE,
        data=[
            remote_data(_CREDENTIALS_SOURCE, key)
            for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    terraform.gitops_terraform(
        chart,
        "terraform",
        name="dns-records",
        variables={
            "route53_zone_id": "Z02901943N8ZFQFOD9P5I",
            # Inline rather than a ConfigMap read through varsFrom: tofu-controller writes
            # spec.vars structurally into the runner's tfvars (a varsFrom value arrives as one
            # string) and reconciles a spec change at once, while a referenced ConfigMap is
            # never watched and waits for the interval.
            "public_nodes": {
                name: {"public_ip": host.public_ip, "role": host.role}
                for name, host in sorted(mesh.public_kubernetes_nodes().items())
            },
        },
        env_from=[terraform.secret_env_from(_CREDENTIALS_SECRET)],
    )
    return chart


def write_manifests(root: Path, mesh: nebula_mesh.Mesh) -> None:
    write_charts(root, OUTPUT_DIR, lambda app: chart(app, mesh))


def dns_automation(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
    external_creds: Kustomization,
    external_secrets_config: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "dns-automation",
        artifact,
        wait=None,
        health_checks=[
            KustomizationSpecHealthChecks(
                api_version="infra.contrib.fluxcd.io/v1alpha2",
                kind="Terraform",
                name="dns-records",
                namespace="flux-system",
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            tofu_controller, tofu_state_db, external_creds, external_secrets_config
        ),
    )
