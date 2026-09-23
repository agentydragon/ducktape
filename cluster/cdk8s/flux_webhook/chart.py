"""Flux's notification wiring: the GitHub webhook Receiver, its public route and the
NetworkPolicy that admits the Gateway to it; the Grafana-annotation and ntfy on-call Alerts
with their Providers, and the Secret that points the ntfy Provider at the self-hosted ntfy.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecRefreshPolicy,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)
from flux_alert_crds.io.fluxcd.toolkit.notification import (
    Alert,
    AlertSpec,
    AlertSpecEventSeverity,
    AlertSpecEventSources,
    AlertSpecEventSourcesKind,
    AlertSpecProviderRef,
)
from flux_provider_crds.io.fluxcd.toolkit.notification import (
    Provider,
    ProviderSpec,
    ProviderSpecSecretRef,
    ProviderSpecType,
)
from flux_receiver_crds.io.fluxcd.toolkit.notification import (
    Receiver,
    ReceiverSpec,
    ReceiverSpecResources,
    ReceiverSpecResourcesKind,
    ReceiverSpecSecretRef,
    ReceiverSpecType,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import ntfy
from cluster.cdk8s.external_secrets.external_secret import add_external_secret, cluster_secret_store, remote_data
from cluster.cdk8s.flux import SOPS_DECRYPTION, Kustomization, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata

NAME = "flux-webhook"
NAMESPACE = "flux-system"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/flux-webhook"
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


def _alert_sources(*sources: tuple[AlertSpecEventSourcesKind, str]) -> list[AlertSpecEventSources]:
    return [AlertSpecEventSources(kind=kind, name="*", namespace=namespace) for kind, namespace in sources]


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    # One URL (the webhookPath is stable across events) for both GitHub event types:
    #   push             -> reconcile the Ducktape GitRepositories (eliminates 1-min poll lag)
    #   registry_package -> reconcile the GHCR ImageRepositories (eliminates 5-min poll lag)
    # Either event reconciles every listed resource, which is harmless: an ImageRepository
    # scan on a git push is a no-op, and so is a GitRepository reconciliation on an image push.
    #
    # terraform/gitops/flux-webhook-token configures the GitHub webhook
    # (github_repository_webhook with events = ["push", "registry_package"]). For
    # registry_package, each GHCR package must be linked to the ducktape repo in GitHub for
    # the event to fire on this repo-level webhook.
    Receiver(
        chart,
        "github-receiver",
        metadata=metadata("github", NAMESPACE),
        spec=ReceiverSpec(
            type=ReceiverSpecType.GITHUB,
            events=["push", "registry_package"],
            secret_ref=ReceiverSpecSecretRef(name="github-webhook-token"),
            resources=[
                ReceiverSpecResources(
                    api_version="source.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.GIT_REPOSITORY,
                    name="flux-system",
                    namespace="flux-system",
                ),
                # Public Flux control objects retain a dedicated sparse checkout. Reconcile
                # it immediately on a Ducktape push rather than waiting for its poll.
                ReceiverSpecResources(
                    api_version="source.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.GIT_REPOSITORY,
                    name="ducktape",
                    namespace="ducktape-flux",
                ),
                ReceiverSpecResources(
                    api_version="image.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.IMAGE_REPOSITORY,
                    name="haku-openclaw-spike",
                ),
                ReceiverSpecResources(
                    api_version="image.toolkit.fluxcd.io/v1",
                    kind=ReceiverSpecResourcesKind.IMAGE_REPOSITORY,
                    name="openclaw",
                ),
            ],
        ),
    )
    grafana = Provider(
        chart,
        "grafana-provider",
        metadata=metadata("grafana", NAMESPACE),
        spec=ProviderSpec(
            type=ProviderSpecType.GRAFANA,
            # notification-controller >=1.7 sends the request to `address` as-is (no
            # auto-append of /api/annotations like older versions did), so the full endpoint
            # path is required here.
            address="https://grafana.allegedly.works/api/annotations",
            secret_ref=ProviderSpecSecretRef(name="grafana-flux-token"),
        ),
    )
    Alert(
        chart,
        "grafana-alert",
        metadata=metadata("grafana-annotations", NAMESPACE),
        spec=AlertSpec(
            provider_ref=AlertSpecProviderRef(name=grafana.name),
            event_severity=AlertSpecEventSeverity.INFO,
            event_sources=_alert_sources(
                (AlertSpecEventSourcesKind.KUSTOMIZATION, "flux-system"),
                (AlertSpecEventSourcesKind.KUSTOMIZATION, "ducktape-flux"),
                (AlertSpecEventSourcesKind.HELM_RELEASE, "flux-system"),
            ),
        ),
    )
    ntfy_provider = Provider(
        chart,
        "ntfy-provider",
        metadata=metadata("ntfy", NAMESPACE),
        spec=ProviderSpec(type=ProviderSpecType.GENERIC, secret_ref=ProviderSpecSecretRef(name=_NTFY_WEBHOOK)),
    )
    Alert(
        chart,
        "ntfy-alert",
        metadata=metadata("on-call", NAMESPACE),
        spec=AlertSpec(
            provider_ref=AlertSpecProviderRef(name=ntfy_provider.name),
            event_severity=AlertSpecEventSeverity.ERROR,
            event_sources=_alert_sources(
                (AlertSpecEventSourcesKind.KUSTOMIZATION, "flux-system"),
                (AlertSpecEventSourcesKind.KUSTOMIZATION, "ducktape-flux"),
                (AlertSpecEventSourcesKind.HELM_RELEASE, "flux-system"),
                (AlertSpecEventSourcesKind.GIT_REPOSITORY, "flux-system"),
                (AlertSpecEventSourcesKind.GIT_REPOSITORY, "ducktape-flux"),
            ),
        ),
    )
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
    add_external_secret(
        chart,
        "ntfy-webhook",
        name=_NTFY_WEBHOOK,
        namespace=NAMESPACE,
        refresh=ExternalSecretSpecRefreshPolicy.ON_CHANGE,
        store=cluster_secret_store(ntfy.SECRET_STORE),
        data=[remote_data("ntfy-credentials", "alertmanager-token", secret_key="alertmanager_token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
            type="Opaque",
            data={"address": f"https://{ntfy.HOSTNAME}/alerts", "headers": _NTFY_HEADERS},
        ),
        annotations={
            "description": "Flux failure notifications delivered through the self-hosted ntfy instance",
            "ntfy.ducktape.io/auth-generation": "1",
        },
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)


def flux_webhook(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    flux_webhook_token: Kustomization,
    ntfy: Kustomization,
    external_secrets_config: Kustomization,
    gateway: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        artifact,
        retry_interval=None,
        wait=None,
        timeout="5m",
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(flux_webhook_token, ntfy, external_secrets_config, gateway),
    )
