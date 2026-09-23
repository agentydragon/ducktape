"""The generated part of `cluster/k8s/flux-webhook`: the public route to Flux's webhook
receiver, the NetworkPolicy that admits the Gateway to it, and the Secret that points the
ntfy notification Provider at the self-hosted ntfy.

Hand-written beside the generated output, since no cdk8s binding covers the
notification-controller kinds: `github-webhook-receiver.yaml` (the Receiver), and the
Providers and Alerts in `ntfy-alerts.yaml` and `grafana-alerts.yaml`.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)

from cluster.cdk8s import ntfy
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

NAME = "flux-webhook"
NAMESPACE = "flux-system"
OUTPUT_DIR = "cluster/k8s/flux-webhook"
_NTFY_WEBHOOK = "ntfy-webhook"
# ntfy fills the X-Title/X-Message placeholders from Flux's webhook payload (Template: yes).
# They are Go raw strings here so ESO's own template engine emits them untouched instead of
# failing on the missing `involvedObject` key.
_NTFY_HEADERS = """\
Template: "yes"
Authorization: "Bearer {{ .alertmanager_token }}"
X-Title: "{{ `{{.involvedObject.kind}} {{.involvedObject.name}}` }}"
X-Message: "{{ `{{.severity}}: {{.reason}} - {{.message}}` }}"
X-Tags: "rotating_light"
"""


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # Handles the GitHub push and registry_package webhooks.
    https_route(
        chart,
        "route",
        metadata=metadata(NAME, NAMESPACE),
        hostname="flux-webhook.allegedly.works",
        backend="webhook-receiver",
        port=80,
        hsts=False,
        listener=None,
    )
    # The default allow-webhooks policy uses a namespaceSelector, which doesn't match
    # Cilium's internal Envoy identity, so this admits any source on the receiver port only.
    k8s.KubeNetworkPolicy(
        chart,
        "gateway-ingress",
        metadata=k8s.ObjectMeta(name="allow-gateway-webhook-ingress", namespace=NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels={"app": "notification-controller"}),
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(9292), protocol="TCP")]
                )
            ],
            policy_types=["Ingress"],
        ),
    )
    ExternalSecret(
        chart,
        "ntfy-webhook",
        metadata=metadata(
            _NTFY_WEBHOOK,
            NAMESPACE,
            annotations={
                "description": "Flux failure notifications delivered through the self-hosted ntfy instance",
                "ntfy.ducktape.io/auth-generation": "1",
            },
        ),
        spec=ExternalSecretSpec(
            refresh_policy=ExternalSecretSpecRefreshPolicy.ON_CHANGE,
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                name=ntfy.SECRET_STORE, kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE
            ),
            target=ExternalSecretSpecTarget(
                name=_NTFY_WEBHOOK,
                creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
                template=ExternalSecretSpecTargetTemplate(
                    engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
                    type="Opaque",
                    data={"address": f"https://{ntfy.HOSTNAME}/alerts", "headers": _NTFY_HEADERS},
                ),
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key="alertmanager_token",
                    remote_ref=ExternalSecretSpecDataRemoteRef(key="ntfy-credentials", property="alertmanager-token"),
                )
            ],
        ),
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
