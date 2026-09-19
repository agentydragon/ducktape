"""The public-coder devbox SSH Service."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import Pods, Protocol, Service, ServicePort, ServiceType
from constructs import Construct

from cluster.cdk8s.metadata import metadata

NAMESPACE = "public-coder-agent"
SERVICE_NAME = "public-coder-devbox-ssh"
VM_NAME = "public-coder-devbox"
SSH_PORT = 22
OUTPUT_DIR = "cluster/k8s/agents/public-coder-agent/devbox"
_SERVICE_LABELS = {"app.kubernetes.io/name": VM_NAME}
_POD_LABELS = {"kubevirt.io/domain": VM_NAME}


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
    """Write the generated Service manifest and return that same cdk8s object."""
    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = Chart(app, VM_NAME, disable_resource_name_hashes=True)
    service = ssh_service(chart)
    app.synth()
    return service
