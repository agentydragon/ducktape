"""Layered Talos cluster bootstrap.

This is the ONLY supported way to bootstrap the cluster.
Run via: bazel run //cluster:bootstrap

Multi-layer deployment with persistent auth separation:
  Layer 0: Persistent Auth (CSI tokens, sealed secrets keypair)
  Layer 1: Infrastructure (VMs, Talos, CNI, networking)
  Layer 2: Flux (GitOps bootstrap - Flux handles DNS/SSO automatically)
"""

import argparse
import contextlib
import json
import logging
import os
import subprocess
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum
from pathlib import Path

import pygit2
from kubernetes import client, config
from kubernetes.client import ApiException
from kubernetes.stream import stream
from kubernetes.stream.ws_client import STDERR_CHANNEL, STDOUT_CHANNEL
from pydantic import BaseModel
from tenacity import Retrying, retry_if_result, stop_after_delay, wait_fixed

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


def parse_json_objects(text: str) -> list[dict]:
    """Parse concatenated JSON objects (talosctl -o json output)."""
    decoder = json.JSONDecoder()
    results = []
    idx = 0
    text = text.strip()
    while idx < len(text):
        try:
            obj, end_idx = decoder.raw_decode(text, idx)
            results.append(obj)
            idx = end_idx
        except json.JSONDecodeError:
            idx += 1
    return results


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

    wait_for_convergence(v1)
    verify_clusterip_routing(v1)
    log.info("Infrastructure layer ready")


def wait_for_convergence(v1: client.CoreV1Api, timeout: int = 600, interval: int = 15) -> None:
    """Wait for KubeSpan peers and Cilium health mesh to converge.

    Deploying webhook-based services (kyverno) before cross-node networking
    converges causes webhook timeout failures from API servers that can't reach
    webhook pods on other nodes.
    """
    log.info("Waiting for cross-node networking to converge...")

    talosconfig = Layer.INFRASTRUCTURE.tf_dir / "talosconfig.yml"
    bootstrap_ip = tofu_output(Layer.INFRASTRUCTURE, "bootstrap_node_ip")
    expected_peers = int(tofu_output(Layer.INFRASTRUCTURE, "expected_node_count")) - 1

    Retrying(
        stop=stop_after_delay(timeout),
        wait=wait_fixed(interval),
        retry=retry_if_result(lambda converged: not converged),
    )(_check_convergence, v1, bootstrap_ip, talosconfig, expected_peers)

    log.info("Cross-node networking converged")


def _check_convergence(v1: client.CoreV1Api, bootstrap_ip: str, talosconfig: Path, expected_peers: int) -> bool:
    """Check KubeSpan peers and Cilium health, return True when converged."""
    result = run(
        ["talosctl", "-n", bootstrap_ip, "--talosconfig", talosconfig, "get", "kubespanpeerstatuses", "-o", "json"],
        capture=True,
        check=False,
    )
    peers = parse_json_objects(result.stdout) if result.returncode == 0 else []
    up_count = sum(1 for p in peers if p.get("spec", {}).get("state") == "up")
    cilium_ok = _check_cilium_health(v1)

    if up_count < expected_peers or not cilium_ok:
        log.info(
            "  KubeSpan: %d/%d peers up (need %d), Cilium healthy: %s", up_count, len(peers), expected_peers, cilium_ok
        )
        return False
    return True


def _check_cilium_health(v1: client.CoreV1Api) -> bool:
    """Check Cilium health from ALL nodes to verify full mesh connectivity.

    A single-pod check only verifies connectivity from that pod's node. During
    bootstrap, other node pairs (e.g., vps-cp-1 → pve-worker-0) may still have
    unstable VXLAN tunnels. Checking from every cilium pod ensures the full mesh
    is healthy before proceeding.
    """
    pods = v1.list_namespaced_pod("kube-system", label_selector="k8s-app=cilium")
    if not pods.items:
        return False
    for pod in pods.items:
        try:
            resp = stream(
                v1.connect_get_namespaced_pod_exec,
                pod.metadata.name,
                "kube-system",
                command=["cilium-health", "status", "-o", "json"],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            resp.run_forever(timeout=30)
            output = resp.read_channel(STDOUT_CHANNEL)
        except ApiException:
            return False
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            stderr = resp.read_channel(STDERR_CHANNEL)
            log.info(
                "  Cilium health unparseable on %s: stdout=%r stderr=%r",
                pod.spec.node_name,
                output[:200],
                stderr[:200] if stderr else "",
            )
            return False
        for node in data.get("nodes", []):
            for section in ("host", "health-endpoint"):
                addr = node.get(section, {}).get("primary-address", {})
                for proto in ("icmp", "http"):
                    status = addr.get(proto, {}).get("status", "")
                    if status != "":
                        log.info(
                            "  Cilium: %s → %s %s/%s: %s",
                            pod.spec.node_name,
                            node.get("name", "?"),
                            section,
                            proto,
                            status,
                        )
                        return False
    return True


def verify_clusterip_routing(v1: client.CoreV1Api, timeout: int = 300, interval: int = 10) -> None:
    """Verify ClusterIP routing works from a pod on every node.

    WHY THIS GATE EXISTS: The preceding _check_cilium_health() only verifies
    the Cilium agent-to-agent health mesh (ICMP/HTTP between cilium pods).
    That passes BEFORE Cilium's BPF service maps are populated — there's a
    3-10s gap where Cilium reports healthy but ClusterIP routing silently
    fails. Without this gate, Flux would deploy services into that window,
    causing webhook timeouts and DNS failures.

    On 2026-02-11, this exact gap caused Kyverno webhook timeouts that
    permanently blocked 49 kustomizations. See docs/lessons_learned/
    2026-02-11-cilium-mtu-cross-node-packet-loss.md for the full analysis.

    WHAT WE TEST: Creates a busybox pod on each node and runs nslookup
    against kubernetes.default.svc.cluster.local. This exercises:
      1. ClusterIP routing to kube-dns (same BPF maps as all other ClusterIPs)
      2. CoreDNS serving DNS queries
      3. The kubernetes service being registered in DNS
    All three are prerequisites for services to function after Flux deploys.
    """
    nodes = v1.list_node().items
    node_names = [n.metadata.name for n in nodes]

    log.info("Verifying ClusterIP routing from %d nodes...", len(node_names))

    namespace = "default"
    pod_map: dict[str, str] = {}  # node_name -> pod_name

    for node_name in node_names:
        pod_name = f"clusterip-test-{node_name}"
        pod_map[node_name] = pod_name
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(name=pod_name, labels={"app": "bootstrap-clusterip-test"}),
            spec=client.V1PodSpec(
                node_selector={"kubernetes.io/hostname": node_name},
                # Tolerate all taints so we schedule on control-plane nodes too.
                tolerations=[client.V1Toleration(operator="Exists")],
                containers=[client.V1Container(name="probe", image="busybox:1.37", command=["sleep", "300"])],
                restart_policy="Never",
                termination_grace_period_seconds=0,
                # Auto-kill after 5 min even if cleanup doesn't run (e.g. SIGKILL).
                active_deadline_seconds=300,
            ),
        )
        _create_pod_replacing_existing(v1, namespace, pod)

    try:
        _wait_for_pods_running(v1, namespace, list(pod_map.values()))

        deadline = time.monotonic() + timeout
        while True:
            failed = [
                node_name
                for node_name, pod_name in pod_map.items()
                if not _probe_clusterip_from_pod(v1, namespace, pod_name)
            ]

            if not failed:
                break

            if time.monotonic() > deadline:
                raise SystemExit(f"ClusterIP routing failed from {', '.join(failed)} after {timeout}s")

            log.info(
                "  ClusterIP unreachable from %d/%d nodes (%s) — retrying in %ds",
                len(failed),
                len(node_names),
                ", ".join(failed),
                interval,
            )
            time.sleep(interval)
    finally:
        for pod_name in pod_map.values():
            with contextlib.suppress(ApiException):
                v1.delete_namespaced_pod(pod_name, namespace, grace_period_seconds=0)

    log.info("ClusterIP routing verified from all %d nodes", len(node_names))


def _create_pod_replacing_existing(v1: client.CoreV1Api, namespace: str, pod: client.V1Pod) -> None:
    """Create a pod, deleting any leftover from a previous bootstrap attempt."""
    name = pod.metadata.name
    try:
        v1.create_namespaced_pod(namespace, pod)
    except ApiException as e:
        if e.status != 409:
            raise
        v1.delete_namespaced_pod(name, namespace, grace_period_seconds=0)
        for _ in range(60):
            try:
                v1.read_namespaced_pod(name, namespace)
                time.sleep(1)
            except ApiException as read_err:
                if read_err.status == 404:
                    break
                raise
        v1.create_namespaced_pod(namespace, pod)


def _wait_for_pods_running(v1: client.CoreV1Api, namespace: str, pod_names: list[str], timeout: int = 120) -> None:
    """Wait for all named pods to reach Running phase."""
    deadline = time.monotonic() + timeout
    while True:
        not_running = []
        for name in pod_names:
            try:
                pod = v1.read_namespaced_pod(name, namespace)
                if pod.status.phase != "Running":
                    not_running.append(f"{name}={pod.status.phase}")
            except ApiException:
                not_running.append(f"{name}=Unknown")

        if not not_running:
            return

        if time.monotonic() > deadline:
            raise SystemExit(f"ClusterIP test pods not Running after {timeout}s: {', '.join(not_running)}")

        time.sleep(5)


def _probe_clusterip_from_pod(v1: client.CoreV1Api, namespace: str, pod_name: str) -> bool:
    """DNS-resolve kubernetes.default.svc from inside a pod.

    Uses nslookup with the FQDN (busybox nslookup ignores resolv.conf search
    domains). Exercises the full ClusterIP path: pod sends DNS query to
    kube-dns ClusterIP (10.96.0.10) → Cilium BPF routes it → CoreDNS responds.
    If BPF service maps aren't populated yet, the query times out.
    """
    try:
        resp = stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=[
                "sh",
                "-c",
                "nslookup kubernetes.default.svc.cluster.local > /dev/null 2>&1 && echo PROBE_OK || echo PROBE_FAIL",
            ],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        resp.run_forever(timeout=15)
        stdout = resp.read_channel(STDOUT_CHANNEL)
        return "PROBE_OK" in stdout
    except ApiException:
        return False


class KustomizationPhase(StrEnum):
    PENDING = "Pending"
    RECONCILING = "Reconciling"
    DEP_WAIT = "DepWait"
    FAILED = "Failed"
    STALLED = "Stalled"
    READY = "Ready"


class FluxCondition(BaseModel):
    """Mirrors metav1.Condition from the Flux kustomize-controller API."""

    type: str
    status: str
    reason: str = ""
    message: str = ""


class ObjectMeta(BaseModel):
    name: str


class KustomizationStatus(BaseModel):
    conditions: list[FluxCondition] = []


class FluxKustomization(BaseModel):
    """Partial model of kustomize.toolkit.fluxcd.io/v1 Kustomization."""

    metadata: ObjectMeta
    status: KustomizationStatus = KustomizationStatus()


@dataclass
class StateChange:
    name: str
    old_phase: KustomizationPhase | None
    new_phase: KustomizationPhase
    message: str = ""


def derive_phase(conditions: Sequence[FluxCondition]) -> KustomizationPhase:
    stalled = next((c for c in conditions if c.type == "Stalled"), None)
    if stalled and stalled.status == "True":
        return KustomizationPhase.STALLED

    ready = next((c for c in conditions if c.type == "Ready"), None)
    if ready is None:
        return KustomizationPhase.PENDING
    if ready.status == "True":
        return KustomizationPhase.READY
    if ready.status == "Unknown":
        return KustomizationPhase.RECONCILING

    # ready.status == "False"
    if ready.reason == "DependencyNotReady":
        return KustomizationPhase.DEP_WAIT

    reconciling = next((c for c in conditions if c.type == "Reconciling"), None)
    if reconciling and reconciling.status == "True":
        return KustomizationPhase.RECONCILING

    return KustomizationPhase.FAILED


def get_ready_condition(ks: FluxKustomization) -> FluxCondition | None:
    return next((c for c in ks.status.conditions if c.type == "Ready"), None)


def update_tracked_state(
    tracked: dict[str, FluxKustomization], items: Sequence[FluxKustomization]
) -> list[StateChange]:
    """Update tracked state from Flux Kustomization items, return phase changes."""
    changes: list[StateChange] = []
    for item in items:
        new_phase = derive_phase(item.status.conditions)
        old = tracked.get(item.metadata.name)
        old_phase = derive_phase(old.status.conditions) if old else None
        if old_phase != new_phase:
            ready_cond = get_ready_condition(item)
            changes.append(
                StateChange(
                    name=item.metadata.name,
                    old_phase=old_phase,
                    new_phase=new_phase,
                    message=(ready_cond.message if ready_cond else ""),
                )
            )
        tracked[item.metadata.name] = item
    return changes


def _print_changes(changes: list[StateChange], elapsed: timedelta) -> None:
    """Print batched state change lines, grouping by transition type."""
    groups: dict[tuple[KustomizationPhase | None, KustomizationPhase], list[str]] = {}
    for s in changes:
        key = (s.old_phase, s.new_phase)
        groups.setdefault(key, []).append(s.name)

    ts = timedelta(seconds=int(elapsed.total_seconds()))
    for (old, new), names in groups.items():
        transition = f"{old} -> {new}" if old else f"-> {new}"
        if len(names) <= 3:
            log.info("%s %s: %s", ts, ", ".join(sorted(names)), transition)
        else:
            log.info("%s %d kustomizations: %s", ts, len(names), transition)

    for s in changes:
        if s.new_phase in (KustomizationPhase.FAILED, KustomizationPhase.STALLED) and s.message:
            log.info("        %s: %s", s.name, s.message)


def _print_summary(tracked: dict[str, FluxKustomization], elapsed: timedelta) -> None:
    counts = Counter(derive_phase(ks.status.conditions) for ks in tracked.values())
    total = len(tracked)
    ready = counts.get(KustomizationPhase.READY, 0)
    parts = [f"{ready}/{total} Ready"]
    for phase in KustomizationPhase:
        if phase == KustomizationPhase.READY:
            continue
        count = counts.get(phase, 0)
        if count > 0:
            parts.append(f"{count} {phase}")
    ts = timedelta(seconds=int(elapsed.total_seconds()))
    log.info("%s Progress: %s", ts, ", ".join(parts))


def _print_final_summary(tracked: dict[str, FluxKustomization], *, success: bool, reason: str = "") -> None:
    if success:
        log.info("All %d kustomizations Ready", len(tracked))
        return

    log.error("Convergence failed: %s", reason)
    not_ready = sorted(
        (ks for ks in tracked.values() if derive_phase(ks.status.conditions) != KustomizationPhase.READY),
        key=lambda ks: ks.metadata.name,
    )
    for ks in not_ready:
        ready_cond = get_ready_condition(ks)
        phase = derive_phase(ks.status.conditions)
        log.error(
            "  %s (%s): %s - %s",
            ks.metadata.name,
            phase,
            ready_cond.reason if ready_cond else "",
            ready_cond.message if ready_cond else "",
        )
    counts = Counter(derive_phase(ks.status.conditions) for ks in tracked.values())
    ready = counts.get(KustomizationPhase.READY, 0)
    log.error("Summary: %d/%d Ready, %d not ready", ready, len(tracked), len(not_ready))


def monitor_flux_convergence(
    *,
    global_timeout: timedelta = timedelta(hours=1),
    poll_interval: timedelta = timedelta(seconds=10),
    stable_failure_window: timedelta = timedelta(minutes=12),
) -> None:
    """Monitor Flux kustomizations until all are Ready or convergence stalls.

    Terminates when:
    1. All kustomizations Ready (success)
    2. Ready count hasn't increased for stable_failure_window (failure)
    3. Global timeout (failure)
    """
    custom_api = client.CustomObjectsApi()

    start = datetime.now(UTC)
    tracked: dict[str, FluxKustomization] = {}
    last_ready_increase = start
    last_successful_poll = start
    high_water_ready = 0
    prev_total = 0
    total_stable_polls = 0
    last_summary_at = start - timedelta(seconds=30)

    while True:
        now = datetime.now(UTC)
        elapsed = now - start
        if elapsed >= global_timeout:
            _print_final_summary(tracked, success=False, reason=f"global timeout ({global_timeout})")
            raise SystemExit("Flux convergence timed out")

        try:
            raw = custom_api.list_namespaced_custom_object(
                group="kustomize.toolkit.fluxcd.io", version="v1", namespace="flux-system", plural="kustomizations"
            )
            last_successful_poll = datetime.now(UTC)
        except ApiException as e:
            if elapsed < timedelta(minutes=1):
                log.debug("API not ready yet: %s", e.reason)
            else:
                log.warning("API error polling kustomizations: %s", e.reason)
            time.sleep(poll_interval.total_seconds())
            continue

        items = [FluxKustomization.model_validate(i) for i in raw.get("items", [])]
        changes = update_tracked_state(tracked, items)

        # Track total count stability (don't declare success during ramp-up)
        if len(tracked) == prev_total:
            total_stable_polls += 1
        else:
            total_stable_polls = 0
            prev_total = len(tracked)

        # Track Ready count high-water mark for staleness detection
        ready_count = sum(
            1 for ks in tracked.values() if derive_phase(ks.status.conditions) == KustomizationPhase.READY
        )
        if ready_count > high_water_ready:
            high_water_ready = ready_count
            last_ready_increase = datetime.now(UTC)

        if changes:
            _print_changes(changes, elapsed)

        # Periodic summary every 30s
        if now - last_summary_at >= timedelta(seconds=30):
            _print_summary(tracked, elapsed)
            last_summary_at = now

        # Success: all Ready and total count stable for at least 2 polls
        if tracked and ready_count == len(tracked) and total_stable_polls >= 2:
            _print_final_summary(tracked, success=True)
            return

        # Stalled: Ready count hasn't increased for stable_failure_window
        # (only evaluate when last poll succeeded recently)
        since_increase = datetime.now(UTC) - last_ready_increase
        since_poll = datetime.now(UTC) - last_successful_poll
        if since_increase >= stable_failure_window and since_poll < poll_interval * 3:
            _print_final_summary(
                tracked,
                success=False,
                reason=f"Ready count stuck at {high_water_ready}/{len(tracked)} for {since_increase}",
            )
            raise SystemExit("Flux convergence stalled")

        time.sleep(poll_interval.total_seconds())


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
