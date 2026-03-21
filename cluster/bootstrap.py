"""Layered Talos cluster bootstrap.

This is the ONLY supported way to bootstrap the cluster.
Run via: bazel run //cluster:bootstrap

Multi-layer deployment with persistent auth separation:
  Layer 0: Persistent Auth (CSI tokens, sealed secrets keypair)
  Layer 1: Infrastructure (VMs, Talos, CNI, networking)
  Layer 2: Flux (GitOps bootstrap - Flux handles DNS/SSO automatically)
"""

import argparse
import json
import logging
import os
import subprocess
from enum import IntEnum
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
TERRAFORM_DIR = SCRIPT_DIR / "terraform"

logging.basicConfig(format="[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S", level=logging.INFO)
log = logging.getLogger(__name__)


class Layer(IntEnum):
    PERSISTENT_AUTH = 0
    INFRASTRUCTURE = 1
    FLUX = 2

    @property
    def tf_dir_name(self) -> str:
        return ["persistent-auth", "infrastructure", "flux"][self.value]

    @property
    def tf_dir(self) -> Path:
        return TERRAFORM_DIR / "bootstrap" / self.tf_dir_name


def run(
    cmd: list[str | Path],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=check, timeout=timeout, capture_output=capture, text=capture)


def tofu(layer: Layer, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return run([_TOFU_BIN, *args], cwd=layer.tf_dir, timeout=timeout)


def tofu_output(layer: Layer, name: str) -> str:
    result = run([_TOFU_BIN, "output", "-raw", name], cwd=layer.tf_dir, capture=True)
    return result.stdout.strip()


def tofu_state_has_resources(layer: Layer) -> bool:
    result = run([_TOFU_BIN, "show", "-json"], cwd=layer.tf_dir, capture=True, check=False)
    if result.returncode != 0:
        return False
    resources = json.loads(result.stdout).get("values", {}).get("root_module", {}).get("resources", [])
    return len(resources) > 0


def preflight(root: Path) -> None:
    log.info("Phase 0: Preflight Validation")

    # Only GitOps-managed paths need to be committed (Flux fetches from git).
    # Terraform, scripts, docs, and bootstrap.py itself run locally.
    repo = pygit2.Repository(root)
    gitops_prefixes = ("cluster/k8s/", "cluster/charts/", "cluster/flux-system/")
    diff = repo.index.diff_to_tree(repo.head.peel(pygit2.Tree))
    dirty = [d.delta.new_file.path for d in diff if d.delta.new_file.path.startswith(gitops_prefixes)]
    if dirty:
        raise SystemExit(f"Uncommitted changes in GitOps paths: {', '.join(dirty)}. Commit or stash before bootstrap.")

    log.info("Running pre-commit validation on cluster files...")
    files = [e.path for e in repo.index if e.path.startswith("cluster/")]
    run(["pre-commit", "run", "--files", *files], cwd=root)

    for layer in Layer:
        log.info("Validating tofu layer: %s", layer.tf_dir_name)
        tofu(layer, "validate")


def deploy_persistent_auth() -> None:
    log.info("Layer 0: Persistent Auth Setup")

    layer = Layer.PERSISTENT_AUTH
    state = layer.tf_dir / "terraform.tfstate"
    if state.exists() and tofu_state_has_resources(layer):
        log.info("Persistent auth already exists - skipping")
        return

    log.info("Deploying persistent auth layer...")
    tofu(layer, "apply", "-auto-approve")
    log.info("Persistent auth layer ready")


def deploy_infrastructure() -> None:
    log.info("Layer 1: Infrastructure Deployment")
    log.info("Deploying infrastructure (VMs, Talos, Cilium, sealed-secrets)...")
    tofu(Layer.INFRASTRUCTURE, "apply", "-auto-approve", timeout=1800)

    kubeconfig = Layer.INFRASTRUCTURE.tf_dir / "kubeconfig"
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
    log.info("Infrastructure layer ready")


def deploy_services() -> None:
    log.info("Layer 2: Services")

    kubeconfig = Layer.INFRASTRUCTURE.tf_dir / "kubeconfig"
    os.environ.setdefault("KUBECONFIG", str(kubeconfig))

    log.info("Deploying services (Flux, Authentik, PowerDNS, Harbor, Gitea, Matrix)...")
    tofu(Layer.FLUX, "apply", "-auto-approve")

    log.info("Flux deployed. Monitoring kustomization convergence...")
    config.load_kube_config(str(kubeconfig))
    monitor_flux_convergence()

    generate(SCRIPT_DIR.parent)

    log.info("Bootstrap complete - all kustomizations converged.")
    print(f"\nAccess cluster: export KUBECONFIG='{kubeconfig}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Layered Talos cluster bootstrap")
    parser.add_argument(
        "--start-from", choices=["infrastructure", "services"], help="Skip earlier layers, start from specified layer"
    )
    args = parser.parse_args()

    # Fix pre-commit/pip compatibility with Nix
    os.environ["PIP_USER"] = "false"
    os.environ["PRE_COMMIT_USE_UV"] = "1"

    root = SCRIPT_DIR.parent

    start_layer = {"infrastructure": Layer.INFRASTRUCTURE, "services": Layer.FLUX}.get(
        args.start_from, Layer.PERSISTENT_AUTH
    )

    if start_layer > Layer.PERSISTENT_AUTH:
        log.info("Starting from layer: %s", args.start_from)

    preflight(root)

    if start_layer <= Layer.PERSISTENT_AUTH:
        deploy_persistent_auth()

    if start_layer <= Layer.INFRASTRUCTURE:
        deploy_infrastructure()

    deploy_services()


if __name__ == "__main__":
    main()
