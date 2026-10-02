"""The External Secrets operator: its Namespace, HelmRepository and HelmRelease, the
self-signed cert-manager Issuer for the webhook's certificate, and its Flux Kustomization.
The CRDs come from the `external-secrets-crds` Kustomization, straight from the upstream
repository.

`values` is an untyped dict: Helm values carry no schema for `cdk8s_import` to ingest.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cert_manager_issuer_crds.io.cert_manager import Issuer, IssuerSpec, IssuerSpecSelfSigned

from cluster.cdk8s import namespaces, node_scheduling
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, https_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa

NAME = "external-secrets"
NAMESPACE = "external-secrets-system"
OUTPUT_DIR = f"{GENERATED_ROOT}/external-secrets/operator"


def _values(webhook_issuer: str) -> dict[str, object]:
    return {
        # CRDs installed separately via external-secrets-crds Kustomization
        # This ensures kustomize-controller has CRDs in its cache
        "installCRDs": False,
        # Two replicas so a lost node fails over to a warm standby instead of waiting
        # for a reschedule; leaderElect is what makes that safe, and the chart defaults
        # it off. Without it both replicas reconcile every ExternalSecret concurrently
        # and race each other's writes — a replica count above 1 is only correct
        # together with this flag.
        "replicaCount": 2,
        "leaderElect": True,
        "concurrent": 8,
        # Size the limit above heap + the mapped binary, not just the heap. This
        # controller never OOM-kills when it runs out: the kernel can always meet the
        # limit by evicting the Go binary's file-backed pages, which the process then
        # immediately faults back in. At 128Mi (67Mi heap for 66 ExternalSecrets, and
        # a ~60Mi binary that no longer fits beside it) that loop ran at 36k
        # refaults/s and read 155 MB/s off the node's containerd disk indefinitely,
        # while the pod reported 1/1 Ready with 0 restarts. On an HDD node that
        # starves containerd's CRI calls past their deadlines and strands every pod
        # scheduled there. See docs/lessons_learned/2026_09_07_eso_memory_limit_thrash.md.
        "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}, "requests": {"cpu": "100m", "memory": "256Mi"}},
        # Both settings below apply to the controller and, separately, to the webhook
        # under `webhook:` — the chart takes no inherited value.
        #
        # Without a priority class this sorts with ordinary workloads under kubelet
        # eviction, which is how the webhook got evicted for using 76Ki of ephemeral
        # storage on a node whose disk was filled by something else entirely. Nothing
        # cluster-wide can reconcile an ExternalSecret while it is gone.
        "priorityClassName": "system-cluster-critical",
        # Admission-path infrastructure belongs on the always-on bare metal next to an
        # API server, as kyverno already is; without a toleration the scheduler cannot
        # consider a control-plane node at all. Deliberately no nodeSelector or
        # affinity: steady-state ESO is small and I/O-light, so this widens the
        # candidate set rather than pinning it.
        "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
        "serviceMonitor": {"enabled": True, "namespace": "monitoring"},
        # Service account used by the Kubernetes-provider ClusterSecretStores to read
        # secrets across namespaces.
        "serviceAccount": {"create": True, "name": "external-secrets"},
        # Webhook certificates come from cert-manager, so the chart's own
        # cert-controller is off.
        "webhook": {
            "create": True,
            "port": 9443,
            "priorityClassName": "system-cluster-critical",
            "tolerations": [node_scheduling.CONTROL_PLANE_TOLERATION],
            "certManager": {
                "enabled": True,
                "cert": {"issuerRef": {"group": "cert-manager.io", "kind": "Issuer", "name": webhook_issuer}},
            },
        },
        "certController": {"create": False},
    }


def chart(app: App) -> Chart:
    chart = Chart(app, "external-secrets-operator", disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.RECOMMEND)
    webhook_issuer = Issuer(
        chart,
        "webhook-issuer",
        metadata=ApiObjectMetadata(name="external-secrets-selfsigned-issuer", namespace=NAMESPACE),
        spec=IssuerSpec(self_signed=IssuerSpecSelfSigned()),
    )
    helm_release(
        chart,
        NAME,
        NAMESPACE,
        repository=https_helm_repository(chart, NAME, NAMESPACE, url="https://charts.external-secrets.io"),
        chart="external-secrets",
        version="2.10.0",
        interval="15m",
        install=RETRY_FAILED_INSTALL,
        values=_values(webhook_issuer.name),
    )
    return chart


def external_secrets_operator(
    chart: Chart, directory: RenderedDirectory, external_secrets_crds: Kustomization, cert_manager: Kustomization
) -> Kustomization:
    name = "external-secrets-operator"
    return flux_kustomization(
        chart,
        name,
        directory,
        interval="10m0s",
        timeout="5m0s",
        depends_on=flux_kustomization_depends_on_many(
            # CRDs must be in kustomize-controller cache first
            external_secrets_crds,
            # ESO uses Issuer resources
            cert_manager,
        ),
    )
