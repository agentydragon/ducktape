"""Synthesize SSH MCP's backend and sshpiper Pipe into their Kustomize directories."""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import Service
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import NAMESPACE, flux_kustomization, flux_kustomization_depends_on, kustomize_kustomization
from cluster.cdk8s.generation import sops_decryption, write_yaml
from cluster.cdk8s.ssh_mcp import backend, config, sshpiper
from cluster.scripts.nebula_mesh import Mesh

_OUTPUT_DIR = "cluster/k8s/ssh-mcp"
_SSHPIPER_OUTPUT_DIR = "cluster/k8s/agents/public-coder-agent/sshpiper"
_KEY_FILES = ("keys-atlas.sops.yaml", "keys-public-coder-devbox.sops.yaml", "keys.sops.yaml")


def ssh_mcp(
    flux_chart: Chart,
    root: Path,
    mesh: Mesh,
    devbox_service: Service,
    external_secrets_config: Kustomization,
    forgejo_images: Kustomization,
) -> Kustomization:
    """Write the generated backend and sshpiper manifests under ``root``."""
    ssh_config = config.load(devbox_service)
    out_dir = root / _OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    app = App(outdir=str(out_dir))
    chart = backend.chart(app, config=ssh_config, mesh=mesh)
    add_fleet_rules(
        chart,
        provided_secrets={
            "ssh-mcp-keys": "keys.sops.yaml",
            "ssh-mcp-keys-public-coder-devbox": "keys-public-coder-devbox.sops.yaml",
            "ssh-mcp-keys-atlas": "keys-atlas.sops.yaml",
        },
        providers=frozenset({"ssh-mcp", "external-secrets-config", "forgejo-images", *_KEY_FILES}),
    )
    app.synth()

    kustomization = flux_kustomization(
        flux_chart,
        config.NAME,
        description="Standalone SSH MCP backend for haku-console and Agentplane staging.",
        spec=KustomizationSpec(
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            path=f"./{_OUTPUT_DIR}",
            prune=True,
            wait=True,
            decryption=sops_decryption(_KEY_FILES),
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=config.NAME, namespace=NAMESPACE
            ),
            depends_on=[
                KustomizationSpecDependsOn(name="ssh-mcp-namespace"),
                flux_kustomization_depends_on(external_secrets_config),
                flux_kustomization_depends_on(forgejo_images),
            ],
        ),
    )
    write_yaml(
        out_dir / "kustomization.yaml",
        kustomize_kustomization(
            namespace=config.NAMESPACE, resources=[f"{config.NAME}.k8s.yaml", *_KEY_FILES], components=["./image-pins"]
        ),
    )

    sshpiper_dir = root / _SSHPIPER_OUTPUT_DIR
    sshpiper_dir.mkdir(parents=True, exist_ok=True)
    pipe_app = App(outdir=str(sshpiper_dir))
    pipe_chart = Chart(pipe_app, "pipe-devbox", disable_resource_name_hashes=True)
    sshpiper.construct(pipe_chart, config=ssh_config, downstream_key=ssh_config.agent_downstream_key)
    pipe_app.synth()
    return kustomization
