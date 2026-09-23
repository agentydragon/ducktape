"""The public-coder-agent namespace (with its default ServiceAccount), and the devbox's SSH
Service, Bazel cache claim and BuildBuddy API key.

The KubeVirt `VirtualMachine` stays hand-written in `virtualmachine.yaml`: no binding exists.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import Namespace, Pods, Protocol, Service, ServicePort, ServiceType, k8s
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecData,
    ExternalSecretSpecDataRemoteRef,
    ExternalSecretSpecSecretStoreRef,
    ExternalSecretSpecSecretStoreRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s.flux import kustomize_kustomization
from cluster.cdk8s.generation import write_yaml
from cluster.cdk8s.metadata import metadata

NAMESPACE = "public-coder-agent"
NAMESPACE_OUTPUT_DIR = "cluster/k8s/agents/public-coder-agent/namespace"
NAMESPACE_MANIFEST = "public-coder-agent.k8s.yaml"
SERVICE_NAME = "public-coder-devbox-ssh"
VM_NAME = "public-coder-devbox"
SSH_PORT = 22
OUTPUT_DIR = "cluster/k8s/agents/public-coder-agent/devbox"
_SERVICE_LABELS = {"app.kubernetes.io/name": VM_NAME}
_POD_LABELS = {"kubevirt.io/domain": VM_NAME}
_NAMESPACE_LABELS = {
    "goldilocks.fairwinds.com/enabled": "true",
    "goldilocks.fairwinds.com/vpa-update-mode": "auto",
    "name": NAMESPACE,
    "rbac.ducktape.io/agent-readable-metadata": "true",
}
_NAMESPACE_ANNOTATIONS = {
    "description": (
        "Second OpenClaw agent, egress-confined to a CONNECT proxy and reachable only through the Authentik proxy "
        "outpost. Opens pull requests against public repositories as agentydragon-agent."
    )
}
_NAMESPACE_RESOURCES = [NAMESPACE_MANIFEST]
_DEVBOX_RESOURCES = ["ssh-host-key.sops.yaml", "virtualmachine.yaml", "public-coder-devbox.k8s.yaml"]


def namespace(scope: Construct) -> Namespace:
    """Create the namespace shared by the public-coder-agent components."""
    return Namespace(
        scope,
        "namespace",
        metadata=ApiObjectMetadata(name=NAMESPACE, labels=_NAMESPACE_LABELS, annotations=_NAMESPACE_ANNOTATIONS),
    )


def default_service_account(scope: Construct, namespace: Namespace) -> None:
    """Give the namespace's `default` ServiceAccount the pull secret for ducktape-ci images.

    ducktape-ci is a private tenant in the in-cluster Forgejo registry; cluster/k8s/forgejo-images
    reflects `forgejo-images-creds` into this namespace. Workloads that don't set their own
    imagePullSecrets (the devbox VM's containerDisk pull) need it here.
    """
    k8s.KubeServiceAccount(
        scope,
        "default-service-account",
        metadata=k8s.ObjectMeta(name="default", namespace=namespace.name),
        image_pull_secrets=[k8s.LocalObjectReference(name="forgejo-images-creds")],
    )


def ssh_service(scope: Construct) -> Service:
    """Create the in-cluster Service that exposes the KubeVirt VM's SSH port."""
    return Service(
        scope,
        "ssh-service",
        metadata=metadata(
            SERVICE_NAME,
            NAMESPACE,
            labels=_SERVICE_LABELS,
            annotations={
                "description": (
                    "ClusterIP for operator port-forward access (e.g. nixos-rebuild switch per "
                    "docs/kubevirt_nixos_vm.md) and for ssh-mcp's public-coder-devbox targets "
                    "(cluster/k8s/ssh-mcp); not public."
                )
            },
        ),
        selector=Pods.select(scope, "devbox-pods", labels=_POD_LABELS),
        ports=[ServicePort(name="ssh", port=SSH_PORT, target_port=SSH_PORT, protocol=Protocol.TCP)],
        type=ServiceType.CLUSTER_IP,
    )


def _bazel_cache_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "bazel-cache",
        metadata=k8s.ObjectMeta(
            name="public-coder-devbox-bazel-cache",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Separately deletable local Bazel output, repository, and action cache for "
                    "public-coder-devbox. Stop the VM before deleting this claim to reset it."
                )
            },
        ),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="lvm-proxmox-hdd-block",
            # The local HDD thin pool has ample capacity for this separately resettable cache.
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("20Gi")}),
            volume_mode="Block",
        ),
    )


def _buildbuddy_api_key(scope: Construct) -> None:
    name = "buildbuddy-api-key"
    ExternalSecret(
        scope,
        "buildbuddy-api-key",
        metadata=metadata(name, NAMESPACE),
        spec=ExternalSecretSpec(
            refresh_interval="1h",
            secret_store_ref=ExternalSecretSpecSecretStoreRef(
                kind=ExternalSecretSpecSecretStoreRefKind.CLUSTER_SECRET_STORE,
                name="kubernetes-external-creds-secret-store",
            ),
            target=ExternalSecretSpecTarget(
                name=name,
                # Reuse the existing Reflector mirror during the staged ownership handoff.
                creation_policy=ExternalSecretSpecTargetCreationPolicy.ORPHAN,
                deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
            ),
            data=[
                ExternalSecretSpecData(
                    secret_key="api-key", remote_ref=ExternalSecretSpecDataRemoteRef(key=name, property="api-key")
                )
            ],
        ),
    )


def write_manifests(root: Path) -> Service:
    """Write the namespace and devbox manifests and return the generated SSH Service."""
    namespace_dir = root / NAMESPACE_OUTPUT_DIR
    namespace_dir.mkdir(parents=True, exist_ok=True)
    namespace_app = App(outdir=str(namespace_dir))
    namespace_chart = Chart(namespace_app, NAMESPACE, disable_resource_name_hashes=True)
    default_service_account(namespace_chart, namespace(namespace_chart))
    namespace_app.synth()
    write_yaml(namespace_dir / "kustomization.yaml", kustomize_kustomization(resources=_NAMESPACE_RESOURCES))

    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = Chart(app, VM_NAME, disable_resource_name_hashes=True)
    service = ssh_service(chart)
    _bazel_cache_claim(chart)
    _buildbuddy_api_key(chart)
    app.synth()
    write_yaml(out_dir / "kustomization.yaml", kustomize_kustomization(resources=_DEVBOX_RESOURCES))
    return service
