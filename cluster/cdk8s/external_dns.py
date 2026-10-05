"""ExternalDNS observes the public Gateway and Route 53 in dry-run mode."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import ExternalSecretSpecTargetCreationPolicy

from cluster.cdk8s import external_creds, namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, remote_data

NAME = "external-dns"
NAMESPACE = NAME
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
_CREDENTIALS_SOURCE = "aws-route53-dns-automation-credentials"
_CREDENTIALS_SECRET = "aws-route53-credentials"


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND)
    k8s.KubeServiceAccount(
        chart, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )
    ExternalSecret(
        chart,
        "credentials",
        metadata=ApiObjectMetadata(name=_CREDENTIALS_SECRET, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[
            remote_data(_CREDENTIALS_SOURCE, key)
            for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
        ],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://kubernetes-sigs.github.io/external-dns/"),
        chart=NAME,
        version="1.22.0",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        values={
            "sources": ["gateway-httproute", "crd"],
            "provider": {"name": "aws"},
            "policy": "upsert-only",
            "registry": "txt",
            "txtOwnerId": "ducktape-allegedly-works",
            "txtPrefix": "external-dns-%{record_type}.",
            "domainFilters": ["allegedly.works"],
            "managedRecordTypes": ["A", "AAAA", "CNAME", "MX", "TXT"],
            "extraArgs": [
                "--dry-run",
                "--zone-id-filter=Z02901943N8ZFQFOD9P5I",
                "--gateway-name=cluster-gateway",
                "--gateway-namespace=gateway-system",
            ],
            "env": [
                {"name": key, "valueFrom": {"secretKeyRef": {"name": _CREDENTIALS_SECRET, "key": key}}}
                for key in ("AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_SECRET_ACCESS_KEY")
            ],
            "nodeSelector": dict(node_scheduling.HIL_OVH_NODE_SELECTOR),
            "resources": {"requests": {"cpu": "25m", "memory": "64Mi"}, "limits": {"cpu": "250m", "memory": "256Mi"}},
        },
    )
    return chart


def external_dns(chart: Chart, directory: RenderedDirectory, external_secrets_operator: Kustomization) -> Kustomization:
    return flux_kustomization(
        chart, NAME, directory, timeout="5m", depends_on=[flux_kustomization_depends_on(external_secrets_operator)]
    )
