"""Talos cluster bootstrap.

This is the ONLY supported way to bootstrap the cluster.
Run via: bazel run //cluster:bootstrap

Single TF root at cluster/terraform/main/ with PG backend. Bootstrap uses
targeted applies to deploy infrastructure first (persistent-auth + VMs + Talos +
Cilium), then health checks, then full apply for everything else (Flux, VMs).

For fresh bootstrap (no cluster / no PG): start a temporary local PG via podman,
bootstrap, then migrate state to in-cluster PG with tofu init -migrate-state.
"""

import argparse
import json
import logging
import os
import subprocess
from pathlib import Path

import pygit2
from kubernetes import client, config

from cluster.flux_convergence import monitor_flux_convergence
from cluster.network_readiness import (
    restart_cilium_operator_gateway_controller,
    verify_clusterip_routing,
    wait_for_cilium_health,
)
from cluster.scripts.generate_claude_kubeconfig import generate
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory

_TOFU_BIN = get_required_path("multitool/tools/tofu/tofu")

SCRIPT_DIR = get_build_workspace_directory() / "cluster"
TF_DIR = SCRIPT_DIR / "terraform" / "main"

logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)

# Resources that must exist before infrastructure can be deployed.
# These are the persistent-auth resources (Proxmox users, tokens, keypairs, PKI).
PERSISTENT_AUTH_TARGETS = [
    "proxmox_virtual_environment_role.persistent",
    "proxmox_virtual_environment_user.persistent",
    "proxmox_virtual_environment_user_token.persistent",
    "tls_private_key.sealed_secrets",
    "tls_self_signed_cert.sealed_secrets",
    "tls_private_key.flux_deploy",
    "null_resource.nebula_ca",
    "null_resource.nebula_node_cert",
    "random_password.attic_jwt_token_raw",
]

# Infrastructure resources — VMs, Talos config, Cilium, k8s secrets.
# Applied after persistent-auth, before Flux.
INFRA_TARGETS = [
    "talos_machine_secrets.cluster",
    "talos_image_factory_schematic.hcloud",
    "talos_image_factory_schematic.proxmox",
    "terraform_data.talos_hcloud_image",
    "tls_private_key.ssh",
    "hcloud_ssh_key.talos",
    "hcloud_firewall.talos",
    "hcloud_server.vps",
    "proxmox_virtual_environment_download_file.talos_disk",
    "proxmox_virtual_environment_file.network_config",
    "proxmox_virtual_environment_vm.talos",
    "talos_machine_configuration_apply.vps",
    "talos_machine_configuration_apply.proxmox",
    "talos_machine_bootstrap.cluster",
    "talos_cluster_kubeconfig.cluster",
    "local_file.kubeconfig",
    "local_file.talosconfig",
    "null_resource.gateway_api_crds",
    "null_resource.wait_for_k8s_api",
    "null_resource.cilium_bootstrap",
    "null_resource.wait_for_nodes_ready",
    "kubernetes_secret.hcloud_csi",
    "kubernetes_secret.sealed_secrets_key",
    "kubernetes_config_map.cluster_info",
    "random_string.key_suffix",
]


def run(
    cmd: list[str | Path],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=check, timeout=timeout, capture_output=capture, text=capture)


def tofu(*args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return run([_TOFU_BIN, *args], cwd=TF_DIR, timeout=timeout)


def tofu_output(name: str) -> str:
    result = run([_TOFU_BIN, "output", "-raw", name], cwd=TF_DIR, capture=True)
    return result.stdout.strip()


def state_has_resources() -> bool:
    result = run([_TOFU_BIN, "show", "-json"], cwd=TF_DIR, capture=True, check=False)
    if result.returncode != 0:
        return False
    resources = json.loads(result.stdout).get("values", {}).get("root_module", {}).get("resources", [])
    return len(resources) > 0


def preflight(root: Path) -> None:
    log.info("Preflight Validation")

    repo = pygit2.Repository(root)
    gitops_prefixes = ("cluster/k8s/", "cluster/charts/", "cluster/flux-system/")
    diff = repo.index.diff_to_tree(repo.head.peel(pygit2.Tree))
    dirty = [d.delta.new_file.path for d in diff if d.delta.new_file.path.startswith(gitops_prefixes)]
    if dirty:
        raise SystemExit(f"Uncommitted changes in GitOps paths: {', '.join(dirty)}. Commit or stash before bootstrap.")

    log.info("Running pre-commit validation on cluster files...")
    files = [e.path for e in repo.index if e.path.startswith("cluster/")]
    run(["pre-commit", "run", "--files", *files], cwd=root)

    log.info("Validating tofu configuration...")
    tofu("validate")


def deploy_persistent_auth() -> None:
    """Deploy persistent-auth resources (Proxmox users, tokens, keypairs, PKI).

    These are idempotent — if they already exist, tofu apply is a no-op.
    On fresh bootstrap, they're created first so infrastructure can use them.
    """
    log.info("Phase 1: Persistent Auth")

    targets = [f"-target={t}" for t in PERSISTENT_AUTH_TARGETS]
    tofu("apply", "-auto-approve", *targets)
    log.info("Persistent auth ready")


def deploy_infrastructure() -> None:
    """Deploy infrastructure (VMs, Talos, Cilium, k8s secrets)."""
    log.info("Phase 2: Infrastructure Deployment")

    targets = [f"-target={t}" for t in INFRA_TARGETS]
    tofu("apply", "-auto-approve", *targets, timeout=1800)

    kubeconfig = TF_DIR / "kubeconfig"
    os.environ["KUBECONFIG"] = str(kubeconfig)

    config.load_kube_config(str(kubeconfig))
    v1 = client.CoreV1Api()

    log.info("Verifying cluster access...")
    version = client.VersionApi().get_code()
    log.info("Kubernetes %s.%s", version.major, version.minor)
    for node in v1.list_node().items:
        conditions = node.status.conditions or []
        ready = any(c.type == "Ready" and c.status == "True" for c in conditions)
        log.info("  %s: %s", node.metadata.name, "Ready" if ready else "NotReady")

    wait_for_cilium_health(v1)
    verify_clusterip_routing(v1)
    restart_cilium_operator_gateway_controller(v1)
    log.info("Infrastructure ready")


def deploy_services() -> None:
    """Deploy everything else (Flux, VMs) via full tofu apply."""
    log.info("Phase 3: Services + VMs")

    kubeconfig = TF_DIR / "kubeconfig"
    os.environ.setdefault("KUBECONFIG", str(kubeconfig))

    log.info("Deploying all remaining resources (Flux, VMs)...")
    tofu("apply", "-auto-approve")

    log.info("Flux deployed. Monitoring kustomization convergence...")
    config.load_kube_config(str(kubeconfig))
    monitor_flux_convergence()

    generate(SCRIPT_DIR.parent)

    log.info("Bootstrap complete - all kustomizations converged.")
    print(f"\nAccess cluster: export KUBECONFIG='{kubeconfig}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Talos cluster bootstrap")
    parser.add_argument(
        "--start-from", choices=["infrastructure", "services"], help="Skip earlier phases, start from specified phase"
    )
    args = parser.parse_args()

    # Fix pre-commit/pip compatibility with Nix
    os.environ["PIP_USER"] = "false"
    os.environ["PRE_COMMIT_USE_UV"] = "1"

    root = SCRIPT_DIR.parent

    start_phase = {"infrastructure": 2, "services": 3}.get(args.start_from, 1)

    if start_phase > 1:
        log.info("Starting from phase: %s", args.start_from)

    preflight(root)

    if start_phase <= 1:
        deploy_persistent_auth()

    if start_phase <= 2:
        deploy_infrastructure()

    deploy_services()


if __name__ == "__main__":
    main()
