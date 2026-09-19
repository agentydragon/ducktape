"""Route 53 records for allegedly.works (tf/gitops/dns-records)."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart

from cluster.cdk8s import terraform
from cluster.cdk8s.generation import write_charts
from cluster.scripts import nebula_mesh


def chart(app: App, mesh: nebula_mesh.Mesh) -> Chart:
    """Route 53 records for allegedly.works (tf/gitops/dns-records)."""
    chart = Chart(app, "dns-records", disable_resource_name_hashes=True)
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
        env_from=[terraform.secret_env_from("aws-route53-credentials")],
    )
    return chart


def write_manifests(root: Path, mesh: nebula_mesh.Mesh) -> None:
    write_charts(root, "cluster/k8s/dns-automation", lambda app: chart(app, mesh))
