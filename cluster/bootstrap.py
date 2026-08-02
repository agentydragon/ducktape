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
import base64
import json
import logging
import os
import socket
import subprocess
from pathlib import Path

import pygit2
from kubernetes import client, config

from cluster.network_readiness import (
    restart_cilium_operator_gateway_controller,
    verify_clusterip_routing,
    wait_for_cilium_health,
)
from util.bazel.runfiles import get_required_path
from util.bazel.workspace import get_build_workspace_directory

_TOFU_BIN = get_required_path("multitool/tools/tofu/tofu")

SCRIPT_DIR = get_build_workspace_directory() / "cluster"
TF_DIR = SCRIPT_DIR / "terraform" / "main"

logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)

# Resources that must exist before infrastructure can be deployed.
# These are persistent Terraform resources (Proxmox users/tokens and Talos
# machine secrets). Nebula identities are durable SOPS inputs, not local-exec
# output, so they intentionally do not appear in this target list.
PERSISTENT_AUTH_TARGETS = [
    "proxmox_virtual_environment_role.persistent",
    "proxmox_virtual_environment_user.persistent",
    "proxmox_virtual_environment_user_token.persistent",
    "talos_machine_secrets.cluster",
]

# Infrastructure resources — machines, Talos config, Cilium, k8s secrets.
# Applied after persistent-auth, before Flux.
INFRA_TARGETS = [
    "talos_image_factory_schematic.proxmox",
    "tls_private_key.ssh",
    "proxmox_virtual_environment_download_file.talos_disk",
    "proxmox_virtual_environment_file.network_config",
    "proxmox_virtual_environment_vm.talos",
    "talos_machine_configuration_apply.proxmox",
    "talos_machine_bootstrap.cluster",
    "talos_cluster_kubeconfig.cluster",
    "local_file.kubeconfig",
    "local_file.talosconfig",
    "null_resource.gateway_api_crds",
    "null_resource.wait_for_k8s_api",
    "null_resource.cilium_bootstrap",
    "null_resource.wait_for_nodes_ready",
    "kubernetes_namespace.flux_system",
    "kubernetes_secret.sops_age_cluster_secrets",
    "kubernetes_config_map.cluster_info",
    # OVH Kimsufi worker nodes (bare metal, rescue→dd→harddisk provisioning)
    "ovh_dedicated_server.kimsufi",
    "ovh_dedicated_server_update.kimsufi_rescue",
    "ovh_dedicated_server_reboot_task.kimsufi_to_rescue",
    "null_resource.install_talos_kimsufi",
    "ovh_dedicated_server_update.kimsufi_harddisk",
    "ovh_dedicated_server_reboot_task.kimsufi_to_talos",
    "talos_machine_configuration_apply.kimsufi",
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


def tofu(*args: str, excludes: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    exclude_flags = [f"-exclude={e}" for e in excludes]
    result = run([_TOFU_BIN, *args, *exclude_flags], cwd=TF_DIR, timeout=timeout, check=False)
    if result.returncode != 0:
        raise SystemExit(f"tofu {args[0]} failed (exit {result.returncode})")
    return result


def tofu_output(name: str) -> str:
    result = run([_TOFU_BIN, "output", "-raw", name], cwd=TF_DIR, capture=True)
    return result.stdout.strip()


def state_has_resources() -> bool:
    result = run([_TOFU_BIN, "show", "-json"], cwd=TF_DIR, capture=True, check=False)
    if result.returncode != 0:
        return False
    resources = json.loads(result.stdout).get("values", {}).get("root_module", {}).get("resources", [])
    return bool(resources)


def preflight(root: Path) -> None:
    log.info("Preflight Validation")

    repo = pygit2.Repository(root)
    gitops_prefixes = ("cluster/k8s/", "cluster/flux-system/")
    diff = repo.index.diff_to_tree(repo.head.peel(pygit2.Tree))
    dirty = [d.delta.new_file.path for d in diff if d.delta.new_file.path.startswith(gitops_prefixes)]
    if dirty:
        raise SystemExit(f"Uncommitted changes in GitOps paths: {', '.join(dirty)}. Commit or stash before bootstrap.")

    log.info("Running pre-commit validation on cluster files...")
    files = [e.path for e in repo.index if e.path.startswith("cluster/")]
    run(["pre-commit", "run", "--files", *files], cwd=root)

    log.info("Validating tofu configuration...")
    tofu("validate", excludes=[])


def deploy_persistent_auth() -> None:
    """Deploy persistent Terraform resources (Proxmox users, tokens, machine secrets).

    These are idempotent — if they already exist, tofu apply is a no-op.
    On fresh bootstrap, they're created first so infrastructure can use them.
    Targeted applies don't need -exclude (tofu doesn't allow combining them,
    and the target lists don't include module.wyrm2).
    """
    log.info("Phase 1: Persistent Auth")

    targets = [f"-target={t}" for t in PERSISTENT_AUTH_TARGETS]
    tofu("apply", "-auto-approve", *targets, excludes=[])

    _export_machine_secrets()
    log.info("Persistent auth ready")


def _export_machine_secrets() -> None:
    """Export machine secrets to SOPS and derive k8s-ca.crt + k8s-worker.yaml.

    SOPS file is the durable source of truth for machine secrets — survives
    tofu state loss. k8s-ca.crt and k8s-worker.yaml are derived from it.
    """
    secrets_dir = (SCRIPT_DIR / ".." / "secrets").resolve()
    sops_file = secrets_dir / "talos-machine-secrets.sops.yaml"
    ca_crt_file = secrets_dir / "k8s-ca.crt"
    worker_file = secrets_dir / "k8s-worker.yaml"
    repo_root = SCRIPT_DIR / ".."

    # Get machine secrets JSON from tofu output
    result = run([_TOFU_BIN, "output", "-raw", "machine_secrets_json"], cwd=TF_DIR, capture=True, check=False)
    if result.returncode != 0:
        log.warning("Could not read machine_secrets_json output — skipping export")
        return

    secrets = json.loads(result.stdout)

    # 1. Export full machine secrets to SOPS
    log.info("Exporting machine secrets to SOPS...")
    sops_file.write_text(result.stdout)
    run(["sops", "-e", "-i", str(sops_file)], cwd=repo_root)
    log.info("  → %s", sops_file)

    # 2. Derive k8s-ca.crt (plaintext PEM)
    ca_pem = base64.b64decode(secrets["certs"]["k8s"]["cert"]).decode()
    ca_crt_file.write_text(ca_pem)
    log.info("  → %s", ca_crt_file)

    # 3. Derive k8s-worker.yaml (SOPS-encrypted bootstrap token)
    token = secrets["secrets"]["bootstrap_token"]
    worker_file.write_text(f"k8s_bootstrap_token: {token}\n")
    run(["sops", "-e", "-i", str(worker_file)], cwd=repo_root)
    log.info("  → %s", worker_file)


def deploy_infrastructure() -> None:
    """Deploy infrastructure (VMs, Talos, Cilium, k8s secrets)."""
    log.info("Phase 2: Infrastructure Deployment")

    targets = [f"-target={t}" for t in INFRA_TARGETS]
    tofu("apply", "-auto-approve", *targets, excludes=[], timeout=1800)

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


def deploy_services(*, excludes: list[str]) -> None:
    """Deploy everything else (Flux, VMs) via full tofu apply."""
    log.info("Phase 3: Services + VMs")

    kubeconfig = TF_DIR / "kubeconfig"
    os.environ.setdefault("KUBECONFIG", str(kubeconfig))

    log.info("Deploying all remaining resources (Flux, VMs)...")
    tofu("apply", "-auto-approve", excludes=excludes)

    log.info("Bootstrap complete.")
    print(f"\nAccess cluster: export KUBECONFIG='{kubeconfig}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Talos cluster bootstrap")
    parser.add_argument(
        "--start-from", choices=["infrastructure", "services"], help="Skip earlier phases, start from specified phase"
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Tofu resource address to exclude from all applies (passed as -exclude= to tofu). Can be repeated.",
    )
    parser.add_argument(
        "--skip-preflight", action="store_true", help="Skip preflight validation (pre-commit, tofu validate)"
    )
    args = parser.parse_args()

    # Safety check: running on wyrm2 without excluding it will cause
    # tofu to apply pending VM config changes, rebooting the machine mid-bootstrap.
    wyrm2_exclude = "proxmox_virtual_environment_vm.wyrm2"
    hostname = socket.gethostname()
    if hostname == "wyrm2" and wyrm2_exclude not in args.exclude:
        raise SystemExit(
            f"Running on wyrm2 without --exclude={wyrm2_exclude}. "
            "Tofu will apply pending VM changes and reboot this machine mid-bootstrap. "
            f"Re-run with: --exclude={wyrm2_exclude}"
        )

    excludes = args.exclude

    # Fix pre-commit/pip compatibility with Nix
    os.environ["PIP_USER"] = "false"
    os.environ["PRE_COMMIT_USE_UV"] = "1"

    root = SCRIPT_DIR.parent

    start_phase = {"infrastructure": 2, "services": 3}.get(args.start_from, 1)

    if start_phase > 1:
        log.info("Starting from phase: %s", args.start_from)

    if not args.skip_preflight:
        preflight(root)

    if start_phase <= 1:
        deploy_persistent_auth()

    if start_phase <= 2:
        deploy_infrastructure()

    deploy_services(excludes=excludes)


if __name__ == "__main__":
    main()
