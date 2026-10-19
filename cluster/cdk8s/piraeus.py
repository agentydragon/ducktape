"""The Piraeus Operator and a two-node, file-backed LINSTOR canary."""

from __future__ import annotations

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import namespaces
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.helm import RETRY_FAILED_INSTALL, helm_release, oci_helm_repository
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAMESPACE = "piraeus-datastore"
OPERATOR_OUTPUT_DIR = f"{GENERATED_ROOT}/piraeus/operator"
CLUSTER_OUTPUT_DIR = f"{GENERATED_ROOT}/piraeus/cluster"

_CANARY_LABEL = "storage.allegedly.works/drbd-canary"
_CANARY_SELECTOR = {_CANARY_LABEL: "true"}
_POOL_DIRECTORY = "/var/mnt/local-path-ovh-hdd/linstor-canary"
_CONTROLLER_POD_SELECTOR = {"app.kubernetes.io/component": "linstor-controller"}
_REACTOR_CONFIG_PATCH = get_required_path(own_repo_rlocation("cluster/cdk8s/piraeus-reactor-patch.yaml")).read_text()


def operator_chart(app: App) -> Chart:
    """Install the pinned operator and its CRDs before the separate cluster chart."""
    chart = Chart(app, "piraeus-operator", disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.RECOMMEND,
        labels={
            "pod-security.kubernetes.io/enforce": "privileged",
            "pod-security.kubernetes.io/audit": "privileged",
            "pod-security.kubernetes.io/warn": "privileged",
        },
    )
    repository = oci_helm_repository(
        chart, "piraeus-operator", "flux-system", url="oci://ghcr.io/piraeusdatastore/piraeus-operator"
    )
    helm_release(
        chart,
        "piraeus-operator",
        NAMESPACE,
        repository=repository,
        chart="piraeus",
        version="2.12.0",
        interval="30m",
        timeout="10m",
        install=RETRY_FAILED_INSTALL,
        values={
            "installCRDs": True,
            # The controller is cluster-wide, but keep it on the existing OVH region.
            "nodeSelector": {"topology.kubernetes.io/region": "hil"},
            # A single operator replica can be restarted during node maintenance.
            "podDisruptionBudget": {"enabled": False},
        },
    )
    return chart


def cluster_chart(app: App) -> Chart:
    """Declare LINSTOR, the Talos satellite patch and the opt-in local canary class."""
    chart = Chart(app, "piraeus-cluster", disable_resource_name_hashes=True)
    k8s.KubeNetworkPolicy(
        chart,
        "controller-api-ingress",
        metadata=k8s.ObjectMeta(
            name="linstor-controller-api",
            namespace=NAMESPACE,
            annotations={"description": "Restrict the LINSTOR controller API to mutually authenticated HTTPS."},
        ),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_CONTROLLER_POD_SELECTOR),
            policy_types=["Ingress"],
            # Cilium's local-host exemption keeps node-local health probes on 3370 working.
            # Ordinary Pods may reach only the API-TLS listener; LINSTOR mTLS authenticates clients.
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(3371), protocol="TCP")]
                )
            ],
        ),
    )
    repository = oci_helm_repository(
        chart, "piraeus-charts", "flux-system", url="oci://ghcr.io/piraeusdatastore/helm-charts"
    )
    helm_release(
        chart,
        "linstor-cluster",
        NAMESPACE,
        repository=repository,
        chart="linstor-cluster",
        version="1.3.2",
        interval="30m",
        timeout="10m",
        install=RETRY_FAILED_INSTALL,
        values={
            # The chart and operator create cert-manager resources and private keys at
            # runtime; no certificate or key material is stored in Git.
            "createInternalTLS": "cert-manager",
            "createApiTLS": "cert-manager",
            "linstorCluster": {
                # Both the controller-side and node-side Piraeus workloads are limited to
                # the two hosts prepared in cluster/terraform/main/drbd-canary.tf.
                "nodeSelector": _CANARY_SELECTOR,
                "properties": [{"name": "TcpPortAutoRange", "value": "7900-7919"}],
                "controller": {"enabled": True},
                "csiController": {"enabled": True},
                "csiNode": {"enabled": True},
                # Keep local PV affinity updates, but omit unrelated NFS and HA/eviction
                # components during this two-node evaluation.
                "affinityController": {"enabled": True},
                "highAvailabilityController": {"enabled": False},
                "nfsServer": {"enabled": False},
            },
            "linstorSatelliteConfigurations": [
                {
                    "name": "talos-canary",
                    "nodeSelector": _CANARY_SELECTOR,
                    # Removing an eligible node must not trigger an automatic evacuation.
                    "deletionPolicy": "Retain",
                    # Reactor metrics are unauthenticated. Monitoring is disabled for
                    # this canary, so keep the endpoint local to the host-network Pod.
                    "patches": [
                        {
                            "target": {"version": "v1", "kind": "ConfigMap", "name": "reactor-config"},
                            "patch": _REACTOR_CONFIG_PATCH,
                        }
                    ],
                    "storagePools": [
                        {
                            "name": "local-canary",
                            # No source.hostDevices: LINSTOR must not prepare a disk. This
                            # directory is on each worker's existing XFS data mount.
                            # Monitor physical use against the 20Gi per-node stop threshold
                            # in cluster/docs/drbd_canary.md; sparse FILE_THIN has no quota.
                            "fileThinPool": {"directory": _POOL_DIRECTORY},
                        }
                    ],
                    "podTemplate": {
                        "spec": {
                            # DRBD uses the host network. Talos kubelet selects node
                            # InternalIPs from 10.42.0.0/16, the Nebula mesh subnet.
                            "hostNetwork": True,
                            "dnsPolicy": "ClusterFirstWithHostNet",
                            # Talos supplies DRBD modules in its system extension. Apply
                            # Piraeus' documented Talos patch to remove Linux module-loader
                            # and systemd assumptions and redirect read-only LVM paths.
                            "initContainers": [
                                {"name": "drbd-shutdown-guard", "$patch": "delete"},
                                {"name": "drbd-module-loader", "$patch": "delete"},
                            ],
                            "volumes": [
                                {"name": "run-systemd-system", "$patch": "delete"},
                                {"name": "run-drbd-shutdown-guard", "$patch": "delete"},
                                {"name": "systemd-bus-socket", "$patch": "delete"},
                                {"name": "lib-modules", "$patch": "delete"},
                                {"name": "usr-src", "$patch": "delete"},
                                {
                                    "name": "etc-lvm-backup",
                                    "hostPath": {"path": "/var/etc/lvm/backup", "type": "DirectoryOrCreate"},
                                },
                                {
                                    "name": "etc-lvm-archive",
                                    "hostPath": {"path": "/var/etc/lvm/archive", "type": "DirectoryOrCreate"},
                                },
                            ],
                        }
                    },
                }
            ],
            "storageClasses": [
                {
                    "name": "local-canary",
                    "reclaimPolicy": "Retain",
                    "volumeBindingMode": "WaitForFirstConsumer",
                    "allowVolumeExpansion": True,
                    "parameters": {
                        "linstor.csi.linbit.com/storagePool": "local-canary",
                        "linstor.csi.linbit.com/placementCount": "1",
                        "linstor.csi.linbit.com/allowRemoteVolumeAccess": "false",
                    },
                }
            ],
        },
    )
    return chart


def operator_kustomization(chart: Chart, directory: RenderedDirectory) -> Kustomization:
    """Install the operator while keeping accidental Flux removal from uninstalling it."""
    return flux_kustomization(
        chart,
        "piraeus-operator",
        directory,
        timeout="15m",
        prune=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
    )


def cluster_kustomization(
    chart: Chart, directory: RenderedDirectory, operator: Kustomization, cert_manager: Kustomization
) -> Kustomization:
    """Apply LINSTOR only after its CRDs and cert-manager are Ready."""
    return flux_kustomization(
        chart,
        "piraeus-cluster",
        directory,
        timeout="15m",
        prune=False,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(operator, cert_manager),
    )
