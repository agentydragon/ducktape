"""The public-coder-agent namespace (with its default ServiceAccount) and the devbox SSH Service."""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import Namespace, Pods, Protocol, Service, ServicePort, ServiceType, k8s
from constructs import Construct

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
_DEVBOX_RESOURCES = [
    "buildbuddy-api-key-eso.yaml",
    "ssh-host-key.sops.yaml",
    "bazel-cache-pvc.yaml",
    "virtualmachine.yaml",
    "public-coder-devbox.k8s.yaml",
]


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
    app.synth()
    write_yaml(out_dir / "kustomization.yaml", kustomize_kustomization(resources=_DEVBOX_RESOURCES))
    return service
