"""Post-infrastructure network readiness checks.

Verifies Cilium health and ClusterIP routing are converged before proceeding
with service deployment.
"""

import contextlib
import json
import logging
import subprocess
import time

from kubernetes import client, watch
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


def restart_cilium_operator_gateway_controller(timeout: int = 120) -> None:
    """Restart the operator after the API settles. If its gateway controller fails its initial
    cache sync during Talos configuration, it stops programming new HTTPRoutes until restarted.
    """
    rollout = ["kubectl", "--namespace=kube-system", "rollout"]
    deployment = "deployment/cilium-operator"
    subprocess.run([*rollout, "restart", deployment], check=True)
    try:
        subprocess.run([*rollout, "status", deployment], timeout=timeout, check=True)
    except subprocess.TimeoutExpired:
        logger.warning("Cilium operator restart did not complete within %ds (continuing anyway)", timeout)


def verify_clusterip_routing(v1: client.CoreV1Api, timeout: int = 300, interval: int = 10) -> None:
    """Probe DNS through the kube-dns Service from every node before deploying Flux.

    The Cilium health mesh can converge before its ClusterIP maps. Deploying Kyverno
    during that 3-10s gap left webhooks unreachable and blocked reconciliation; see
    docs/lessons_learned/2026_02_11_cilium_mtu_cross_node_packet_loss.md.
    """
    node_names = [node.metadata.name for node in v1.list_node().items]
    logger.info("Verifying ClusterIP routing from %d nodes...", len(node_names))

    namespace = "default"
    pod_to_node: dict[str, str] = {}
    # Kubernetes enforces the timeout with activeDeadlineSeconds, including a hung nslookup.
    probe_cmd = f"until nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1; do sleep {interval}; done"

    try:
        for node_name in node_names:
            pod_name = f"clusterip-test-{node_name}"
            pod = client.V1Pod(
                metadata=client.V1ObjectMeta(name=pod_name, labels={"app": "bootstrap-clusterip-test"}),
                spec=client.V1PodSpec(
                    node_selector={"kubernetes.io/hostname": node_name},
                    # Tolerate all taints so we schedule on control-plane nodes too.
                    tolerations=[client.V1Toleration(operator="Exists")],
                    containers=[
                        client.V1Container(name="probe", image="busybox:1.37", command=["sh", "-c", probe_cmd])
                    ],
                    restart_policy="Never",
                    termination_grace_period_seconds=0,
                    active_deadline_seconds=timeout,
                ),
            )
            _create_pod_replacing_existing(v1, namespace, pod)
            pod_to_node[pod_name] = node_name
        _wait_for_pods_terminal(v1, namespace, pod_to_node, timeout=timeout + 30)
    finally:
        for pod_name in pod_to_node:
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


def _wait_for_pods_terminal(v1: client.CoreV1Api, namespace: str, pod_to_node: dict[str, str], timeout: int) -> None:
    """Wait for all probe pods to reach a terminal phase (Succeeded or Failed)."""
    remaining = set(pod_to_node)

    w = watch.Watch()
    for event in w.stream(
        v1.list_namespaced_pod, namespace, label_selector="app=bootstrap-clusterip-test", timeout_seconds=timeout
    ):
        pod = event["object"]
        pod_name = pod.metadata.name
        if pod_name not in remaining:
            continue

        phase = pod.status.phase
        if phase == "Succeeded":
            remaining.discard(pod_name)
            if not remaining:
                w.stop()
        elif phase == "Failed":
            w.stop()
            raise RuntimeError(f"ClusterIP routing probe failed on {pod_to_node[pod_name]}")

    if remaining:
        nodes = [pod_to_node[p] for p in remaining]
        raise RuntimeError(f"ClusterIP probe timed out on: {', '.join(nodes)}")
