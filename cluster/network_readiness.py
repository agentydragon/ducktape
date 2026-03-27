"""Post-infrastructure network readiness checks.

Verifies Nebula mesh, Cilium health, and ClusterIP routing are converged
before proceeding with service deployment.
"""

import contextlib
import json
import logging
import time
from datetime import UTC, datetime

from kubernetes import client
from kubernetes.client import ApiException
from kubernetes.stream import stream
from kubernetes.stream.ws_client import STDERR_CHANNEL, STDOUT_CHANNEL
from tenacity import Retrying, retry_if_result, stop_after_delay, wait_fixed

logger = logging.getLogger(__name__)


def wait_for_cilium_health(v1: client.CoreV1Api, timeout: int = 600, interval: int = 15) -> None:
    """Wait for Cilium health mesh to converge.

    Deploying webhook-based services (kyverno) before cross-node networking
    converges causes webhook timeout failures from API servers that can't reach
    webhook pods on other nodes.

    Nebula handles inter-node mesh connectivity. This function only waits for
    the Cilium health mesh (ICMP/HTTP between cilium pods on all nodes).
    """
    logger.info("Waiting for Cilium health mesh to converge...")

    Retrying(
        stop=stop_after_delay(timeout), wait=wait_fixed(interval), retry=retry_if_result(lambda healthy: not healthy)
    )(_check_cilium_health, v1)

    logger.info("Cilium health mesh converged")


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
            logger.info(
                "  Cilium health unparseable on %s: stdout=%r stderr=%r",
                pod.spec.node_name,
                output[:200],
                stderr[:200] if stderr else "",
            )
            return False
        for node in data.get("nodes") or []:
            for section in ("host", "health-endpoint"):
                addr = node.get(section, {}).get("primary-address", {})
                for proto in ("icmp", "http"):
                    status = addr.get(proto, {}).get("status", "")
                    if status != "":
                        logger.info(
                            "  Cilium: %s → %s %s/%s: %s",
                            pod.spec.node_name,
                            node.get("name", "?"),
                            section,
                            proto,
                            status,
                        )
                        return False
    return True


def restart_cilium_operator_gateway_controller(v1: client.CoreV1Api, timeout: int = 120) -> None:
    """Restart the Cilium operator to ensure the gateway controller starts cleanly.

    The gateway controller is a one-shot job inside the operator. If it fails to sync
    the API cache during startup (e.g., because kube-apiserver was restarting during
    talos_machine_configuration_apply), it dies permanently for that pod's lifetime.
    Existing Envoy routes continue serving but new HTTPRoutes are never programmed.

    Restarting the operator here is cheap (~30s) and guarantees the gateway controller
    starts with a healthy apiserver after tofu apply completes.
    """
    logger.info("Restarting Cilium operator to ensure gateway controller starts cleanly...")
    apps_v1 = client.AppsV1Api()
    deployment = apps_v1.read_namespaced_deployment("cilium-operator", "kube-system")
    annotations = deployment.spec.template.metadata.annotations or {}
    annotations["kubectl.kubernetes.io/restartedAt"] = datetime.now(UTC).isoformat()
    deployment.spec.template.metadata.annotations = annotations
    apps_v1.patch_namespaced_deployment("cilium-operator", "kube-system", deployment)

    deadline = time.time() + timeout
    while time.time() < deadline:
        dep = apps_v1.read_namespaced_deployment("cilium-operator", "kube-system")
        if dep.status.updated_replicas == dep.spec.replicas and dep.status.available_replicas == dep.spec.replicas:
            logger.info("Cilium operator restarted and ready")
            return
        time.sleep(5)
    logger.warning("Cilium operator restart did not complete within %ds (continuing anyway)", timeout)


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

    logger.info("Verifying ClusterIP routing from %d nodes...", len(node_names))

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

            logger.info(
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

    logger.info("ClusterIP routing verified from all %d nodes", len(node_names))


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
