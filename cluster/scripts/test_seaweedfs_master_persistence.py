"""Rehearse the pinned operator on disposable real Kubernetes.

One K3s node hosts three Raft members with independent local-path PVCs. This
exercises controller adoption and disk recovery, while the live cluster's
three-node SSD placement and hard hostname anti-affinity need a separate check.
Like agentplane/x/k3s_env, this fixture uses node-IP API/DNS and headless
Services because the RBE kernel lacks kube-proxy's MARK target. ClusterIP
routing and Service port translation are outside this rehearsal's coverage.
The fixture uses Kubernetes 1.34.1; the live cluster is on 1.35.1.
"""

import io
import json
import logging
import os
import tarfile
import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
import pytest_bazel
import yaml
from botocore.config import Config
from testcontainers.community.k3s import K3SContainer

from cluster.scripts.seaweedfs_master_persistence import prepare_member, prepare_statefulset
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)
CRDS = {
    "adminscript": "adminscripts",
    "bucket": "buckets",
    "resourcereferencegrant": "resourcereferencegrants",
    "s3credentials": "s3credentials",
    "s3identity": "s3identities",
    "s3policy": "s3policies",
    "s3policybinding": "s3policybindings",
    "seaweed": "seaweeds",
    "bucketlifecyclepolicy": "bucketlifecyclepolicies",
    "s3oidcprovider": "s3oidcproviders",
    "seaweedbackup": "seaweedbackups",
    "seaweedcsidriver": "seaweedcsidrivers",
    "seaweedrestore": "seaweedrestores",
}


def assert_manifest_fields(expected: Any, actual: Any) -> None:
    """Check authored fields while allowing API defaults and the injected service-account volume."""
    if isinstance(expected, dict):
        for key, value in expected.items():
            # Kubernetes omits empty string fields such as volumeMount.subPath.
            if value == "" and key not in actual:
                continue
            assert key in actual
            assert_manifest_fields(value, actual[key])
    elif (
        isinstance(expected, list) and expected and all(isinstance(item, dict) and "name" in item for item in expected)
    ):
        for item in expected:
            (found,) = [candidate for candidate in actual if candidate["name"] == item["name"]]
            assert_manifest_fields(item, found)
    else:
        assert actual == expected


class Rehearsal:
    def __init__(self, container: K3SContainer) -> None:
        self.container = container

    def node_ip(self) -> str:
        (node,) = self.get("nodes")["items"]
        (address,) = [address["address"] for address in node["status"]["addresses"] if address["type"] == "InternalIP"]
        result: str = address
        return result

    def configure_dns_and_api(self) -> None:
        """Use the established bare-K3s compensation for the executor's absent MARK target."""
        node_ip = self.node_ip()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            deployments = json.loads(self.kubectl("get", "deployments", "-n", "kube-system", "-o", "json"))["items"]
            if {"coredns", "local-path-provisioner"}.issubset({item["metadata"]["name"] for item in deployments}):
                break
            time.sleep(1)
        else:
            pytest.fail("K3s did not create its DNS and storage Deployments")
        for name in ("coredns", "local-path-provisioner"):
            self.kubectl(
                "-n",
                "kube-system",
                "set",
                "env",
                f"deployment/{name}",
                f"KUBERNETES_SERVICE_HOST={node_ip}",
                "KUBERNETES_SERVICE_PORT=6443",
            )
        corefile = self.kubectl("-n", "kube-system", "get", "configmap/coredns", "-o", "jsonpath={.data.Corefile}")
        self.kubectl(
            "-n",
            "kube-system",
            "patch",
            "configmap/coredns",
            "--type=merge",
            "-p",
            json.dumps({"data": {"Corefile": corefile.replace(".:53 {", f".:53 {{\n    bind {node_ip}", 1)}}),
        )
        self.kubectl(
            "-n",
            "kube-system",
            "patch",
            "deployment/coredns",
            "--type=merge",
            "-p",
            json.dumps({"spec": {"template": {"spec": {"hostNetwork": True, "dnsPolicy": "Default"}}}}),
        )
        for name in ("coredns", "local-path-provisioner"):
            self.kubectl("-n", "kube-system", "rollout", "status", f"deployment/{name}", "--timeout=90s")

    def kubectl(self, *args: str, manifest: dict[str, Any] | None = None, check: bool = True) -> str:
        command = ["/bin/kubectl", *args]
        if manifest is not None:
            payload = json.dumps(manifest).encode()
            with io.BytesIO() as buffer:
                with tarfile.open(fileobj=buffer, mode="w") as archive:
                    entry = tarfile.TarInfo("rehearsal-manifest.json")
                    entry.size = len(payload)
                    archive.addfile(entry, io.BytesIO(payload))
                self.container.get_wrapped_container().put_archive("/tmp", buffer.getvalue())
            command = ["/bin/kubectl", *["/tmp/rehearsal-manifest.json" if arg == "-" else arg for arg in args]]
        result = self.container.get_wrapped_container().exec_run(command)
        output: str = result.output.decode()
        if check and result.exit_code:
            raise RuntimeError(f"kubectl {args} exited {result.exit_code}: {output}")
        return output

    def create(self, manifest: dict[str, Any]) -> dict[str, Any]:
        admitted: dict[str, Any] = json.loads(self.kubectl("create", "-f", "-", "-o", "json", manifest=manifest))
        print(f"Created {manifest['kind']}/{manifest['metadata']['name']}", flush=True)
        return admitted

    def patch(self, kind: str, name: str, patch: dict[str, Any]) -> None:
        self.kubectl("patch", kind, name, "--type=merge", "-p", json.dumps(patch))

    def master_json(self, name: str, endpoint: str) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(
            self.kubectl(
                "exec", name, "-c", "master", "--", "wget", "-qO-", "--timeout=10", f"http://127.0.0.1:9333/{endpoint}"
            )
        )
        return result

    def wait_ready(self, kind: str, name: str, replicas: int) -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            objects = [item for item in self.get(kind)["items"] if item["metadata"]["name"] == name]
            if objects and objects[0].get("status", {}).get("readyReplicas", 0) == replicas:
                return
            time.sleep(2)
        pytest.fail(f"{kind}/{name} did not reach {replicas} Ready replicas within 180s")

    def get(self, kind: str, name: str | None = None) -> dict[str, Any]:
        args = ["get", kind, "-o", "json"]
        if name is not None:
            args.append(name)
        result: dict[str, Any] = json.loads(self.kubectl(*args))
        return result

    def archive(self, phase: str) -> None:
        destination = undeclared_outputs_dir() / phase
        destination.mkdir(exist_ok=True)
        (destination / "k3s.log").write_bytes(self.container.get_wrapped_container().logs())
        cgroups = self.container.get_wrapped_container().exec_run(
            ["sh", "-ec", "cat /proc/self/cgroup; cat /proc/self/mountinfo; ls -l /sys/fs/cgroup"]
        )
        (destination / "cgroups.txt").write_bytes(cgroups.output)
        forward = self.container.get_wrapped_container().exec_run(
            ["sh", "-ec", "if [ -e /tmp/s3-portforward.log ]; then cat /tmp/s3-portforward.log; fi"]
        )
        (destination / "s3-portforward.log").write_bytes(forward.output)
        for resource in ("pods", "statefulsets", "pvc", "pv", "events", "seaweeds", "services"):
            (destination / f"{resource}.yaml").write_text(
                self.kubectl("get", resource, "-A", "-o", "yaml", check=False)
            )
        pods = self.get("pods")["items"]
        for pod in pods:
            name = pod["metadata"]["name"]
            (destination / f"{name}.log").write_text(self.kubectl("logs", name, "--all-containers", check=False))


@pytest.fixture
def rehearsal() -> Iterator[Rehearsal]:
    container = K3SContainer("rancher/k3s:v1.34.1-k3s1")
    container.with_exposed_ports(30033)
    # RBE's Firecracker kernel lacks CONFIG_KEYS. The kubelet nevertheless opens
    # these sysctls; shadow the kernel sysctl directory inside this disposable container.
    startup = """
if [ ! -e /proc/sys/kernel/keys/root_maxkeys ]; then
  mkdir -p /tmp/kernel
  cp -r /proc/sys/kernel/. /tmp/kernel/ 2>/dev/null || :
  mkdir -p /tmp/kernel/keys
  echo 1000000 > /tmp/kernel/keys/root_maxkeys
  echo 25000000 > /tmp/kernel/keys/root_maxbytes
  mount --bind /tmp/kernel /proc/sys/kernel
fi
node_ip="$(hostname -i)"
exec k3s server --disable traefik,metrics-server,servicelb --disable-kube-proxy --flannel-backend=host-gw --disable-network-policy --node-ip="$node_ip" --kubelet-arg="cluster-dns=$node_ip"
"""
    # The host cgroup mount and its namespace must agree for nested runc to
    # create real cgroup v2 children rather than plain directories without files.
    container.with_kwargs(entrypoint=["/bin/sh", "-ec"], privileged=True, cgroupns="host")
    container.with_command([startup])
    with container:
        cluster = Rehearsal(container)
        try:
            cluster.kubectl("wait", "node", "--all", "--for=condition=Ready", "--timeout=120s")
            cluster.configure_dns_and_api()
            yield cluster
        finally:
            cluster.archive("final")


def install_operator(cluster: Rehearsal) -> None:
    for repository, plural in CRDS.items():
        path = get_required_path(f"seaweed_{repository}_crd/file/seaweed.seaweedfs.com_{plural}.yaml")
        cluster.create(yaml.safe_load(path.read_text()))
    cluster.kubectl("wait", "crd", "--all", "--for=condition=Established", "--timeout=60s")
    role = yaml.safe_load(get_required_path("seaweed_operator_role/file/role.yaml").read_text())
    cluster.create(role)
    cluster.create({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "operator"}})
    cluster.create(
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "operator"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "manager-role"},
            "subjects": [{"kind": "ServiceAccount", "name": "operator", "namespace": "default"}],
        }
    )
    cluster.create(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "operator"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "operator"}},
                "template": {
                    "metadata": {"labels": {"app": "operator"}},
                    "spec": {
                        "serviceAccountName": "operator",
                        "containers": [
                            {
                                "name": "operator",
                                "image": "chrislusf/seaweedfs-operator:1.0.39",
                                "command": ["/manager"],
                                "args": ["--metrics-bind-address=0", "--leader-elect=false"],
                                "env": [
                                    {"name": "ENABLE_WEBHOOKS", "value": "false"},
                                    {"name": "KUBERNETES_SERVICE_HOST", "value": cluster.node_ip()},
                                    {"name": "KUBERNETES_SERVICE_PORT", "value": "6443"},
                                ],
                                "readinessProbe": {"httpGet": {"path": "/readyz", "port": 8081}, "periodSeconds": 2},
                            }
                        ],
                    },
                },
            },
        }
    )
    cluster.kubectl("rollout", "status", "deployment/operator", "--timeout=120s")


def test_seaweedfs_master_persistence(rehearsal: Rehearsal) -> None:
    (undeclared_outputs_dir() / "known-limit-sigstop.txt").write_text(
        "Prior invocation d36367cb-aa14-48fe-ac81-a0b5fafc5d3f elected a surviving Raft leader "
        "during SIGSTOP, but S3 PUT timed out while filer/volume clients retained TCP connections "
        "to the stopped process. Seaweed 4.46 gRPC keepalive uses 60s idle plus 20s ping timeout. "
        "That blackhole case did not pass and is outside this planned graceful Pod termination rehearsal.\n"
    )
    install_operator(rehearsal)
    rehearsal.create(
        {
            "apiVersion": "seaweed.seaweedfs.com/v1",
            "kind": "Seaweed",
            "metadata": {"name": "rehearsal", "namespace": "default"},
            "spec": {
                "image": "chrislusf/seaweedfs:4.46",
                "volumeServerDiskCount": 1,
                "master": {"replicas": 3, "volumeSizeLimitMB": 64},
                "volume": {"replicas": 2, "requests": {"storage": "1Gi"}},
                "filer": {
                    "replicas": 1,
                    "config": '[leveldb2]\nenabled = true\ndir = "/data/filerldb2"\n',
                    "service": {"clusterIP": "None"},
                },
                "s3": {"replicas": 1},
            },
        }
    )
    rehearsal.wait_ready("statefulsets", "rehearsal-master", 3)
    rehearsal.wait_ready("statefulsets", "rehearsal-volume", 2)
    rehearsal.wait_ready("statefulsets", "rehearsal-filer", 1)
    rehearsal.wait_ready("deployments", "rehearsal-s3", 1)
    rehearsal.archive("ephemeral-ready")

    assert rehearsal.get("service", "rehearsal-filer")["spec"]["clusterIP"] == "None"
    (s3_pod,) = [pod for pod in rehearsal.get("pods")["items"] if pod["metadata"]["name"].startswith("rehearsal-s3-")]
    rehearsal.container.get_wrapped_container().exec_run(
        [
            "sh",
            "-ec",
            f"exec /bin/kubectl port-forward --address=0.0.0.0 pod/{s3_pod['metadata']['name']} 30033:8333 >/tmp/s3-portforward.log 2>&1",
        ],
        detach=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        forward = rehearsal.container.get_wrapped_container().exec_run(["cat", "/tmp/s3-portforward.log"])
        if b"Forwarding from 0.0.0.0:30033" in forward.output:
            break
        time.sleep(1)
    else:
        pytest.fail("S3 Pod port-forward did not start")
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{rehearsal.container.get_container_host_ip()}:{rehearsal.container.get_exposed_port(30033)}",
        aws_access_key_id="rehearsal",
        aws_secret_access_key="rehearsal",
        config=Config(
            connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2}, s3={"addressing_style": "path"}
        ),
    )
    bucket = "migration-rehearsal"
    baseline = os.urandom(256 * 1024)
    s3.create_bucket(Bucket=bucket)
    s3.put_object(Bucket=bucket, Key="baseline", Body=baseline)

    def verify_s3(phase: str) -> None:
        assert s3.get_object(Bucket=bucket, Key="baseline")["Body"].read() == baseline
        payload = os.urandom(256 * 1024)
        s3.put_object(Bucket=bucket, Key=phase, Body=payload)
        assert s3.get_object(Bucket=bucket, Key=phase)["Body"].read() == payload

    verify_s3("ephemeral")
    names = [f"rehearsal-master-{ordinal}" for ordinal in range(3)]
    original_uids = {name: rehearsal.get("pod", name)["metadata"]["uid"] for name in names}
    initial_status = rehearsal.master_json(names[0], "cluster/status")
    old_leader = initial_status["Leader"].split(".")[0]
    topology_id = rehearsal.master_json(old_leader, "dir/status")["TopologyId"]
    maximum_volume_id = rehearsal.master_json(old_leader, "cluster/status")["MaxVolumeId"]
    assert topology_id
    assert maximum_volume_id > 0

    rehearsal.patch(
        "seaweed",
        "rehearsal",
        {
            "spec": {
                "master": {
                    "persistence": {
                        "enabled": True,
                        "mountPath": "/data",
                        "storageClassName": "local-path",
                        "resources": {"requests": {"storage": "4Gi"}},
                    }
                }
            }
        },
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        operator_logs = rehearsal.kubectl("logs", "deployment/operator")
        if "volumeMounts" in operator_logs and "Not found" in operator_logs:
            break
        time.sleep(2)
    else:
        pytest.fail("The pinned operator did not reproduce its missing-volume mount update failure")
    assert {name: rehearsal.get("pod", name)["metadata"]["uid"] for name in names} == original_uids
    rehearsal.archive("operator-update-rejected")

    rehearsal.kubectl("scale", "deployment/operator", "--replicas=0")
    rehearsal.kubectl("wait", "pods", "-l", "app=operator", "--for=delete", "--timeout=60s")
    rehearsal.patch(
        "statefulset", "rehearsal-master", {"spec": {"updateStrategy": {"type": "OnDelete", "rollingUpdate": None}}}
    )
    replacement = prepare_statefulset(
        rehearsal.get("statefulset", "rehearsal-master"), rehearsal.get("seaweed", "rehearsal")
    )
    rehearsal.kubectl("delete", "statefulset", "rehearsal-master", "--cascade=orphan", "--wait=true")
    assert {name: rehearsal.get("pod", name)["metadata"]["uid"] for name in names} == original_uids
    assert not [
        item for item in rehearsal.get("statefulsets")["items"] if item["metadata"]["name"] == "rehearsal-master"
    ]
    assert all(
        not any(owner.get("controller") for owner in rehearsal.get("pod", name)["metadata"].get("ownerReferences", []))
        for name in names
    )
    rehearsal.archive("orphaned-ephemeral")
    verify_s3("orphaned")
    admitted_members: dict[str, dict[str, Any]] = {}
    member_manifests = {name: prepare_member(replacement, ordinal) for ordinal, name in enumerate(names)}

    def wait_master_agreement(members: list[str], timeout_seconds: int = 90) -> str:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                statuses = [rehearsal.master_json(member, "cluster/status") for member in members]
            except RuntimeError:
                logger.warning("Master HTTP request failed while waiting for Raft agreement", exc_info=True)
                time.sleep(2)
                continue
            leaders = {status.get("Leader") for status in statuses}
            if (
                len(leaders) == 1
                and None not in leaders
                and all(status.get("MaxVolumeId", 0) >= maximum_volume_id for status in statuses)
                and len({status.get("MaxVolumeId") for status in statuses}) == 1
                and sum(bool(status.get("IsLeader")) for status in statuses) == 1
            ):
                leader: str = statuses[0]["Leader"].split(".")[0]
                if leader in members:
                    try:
                        observed_topology_id = rehearsal.master_json(leader, "dir/status")["TopologyId"]
                    except RuntimeError:
                        logger.warning("Leader HTTP request failed while waiting for Raft agreement", exc_info=True)
                    else:
                        if observed_topology_id == topology_id:
                            return leader
            time.sleep(2)
        pytest.fail(
            f"Masters did not agree on leader, TopologyId and allocated maximum volume ID within {timeout_seconds}s"
        )

    def raft_files(name: str, phase: str) -> bytes:
        paths = rehearsal.kubectl("exec", name, "-c", "master", "--", "find", "/data", "-type", "f", "-size", "+0c")
        assert "/data/" in paths
        assert "/log" in paths
        (undeclared_outputs_dir() / f"{phase}-raft-files.txt").write_text(paths)
        result = rehearsal.container.get_wrapped_container().exec_run(
            ["/bin/kubectl", "exec", name, "-c", "master", "--", "cat", "/data/m9333/log"]
        )
        assert result.exit_code == 0
        body: bytes = result.output
        assert topology_id.encode() in body
        (undeclared_outputs_dir() / f"{phase}-raft-log.bin").write_bytes(body)
        return body

    def replace_master(name: str, phase: str, *, hold_out_until_quorum: bool = False) -> bytes:
        assert not [
            item for item in rehearsal.get("statefulsets")["items"] if item["metadata"]["name"] == "rehearsal-master"
        ]
        assert rehearsal.get("deployment", "operator")["spec"]["replicas"] == 0
        leader = wait_master_agreement(names)
        for survivor in names:
            if survivor != name:
                rehearsal.kubectl("wait", f"pod/{survivor}", "--for=condition=Ready", "--timeout=30s")
        (undeclared_outputs_dir() / f"{phase}-before-role.json").write_text(
            json.dumps({"member": name, "leader": leader, "restarting_leader": name == leader}, indent=2)
        )
        claim_manifest, pod_manifest = member_manifests[name]
        if name not in admitted_members:
            rehearsal.create(claim_manifest)
        previous_uid = rehearsal.get("pod", name)["metadata"]["uid"]
        deleted = json.loads(
            rehearsal.kubectl(
                "delete",
                "--raw",
                f"/api/v1/namespaces/default/pods/{name}",
                "-f",
                "-",
                manifest={"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": previous_uid}},
            )
        )
        assert deleted["metadata"]["uid"] == previous_uid
        rehearsal.kubectl("wait", f"pod/{name}", "--for=delete", "--timeout=60s")
        assert name not in {pod["metadata"]["name"] for pod in rehearsal.get("pods")["items"]}
        if hold_out_until_quorum:
            survivors = [member for member in names if member != name]
            elected = wait_master_agreement(survivors, timeout_seconds=45)
            assert elected in survivors
            if leader == name:
                assert elected != leader
            verify_s3(f"{phase}-absent")
            assert name not in {pod["metadata"]["name"] for pod in rehearsal.get("pods")["items"]}
            (undeclared_outputs_dir() / f"{phase}-quorum.json").write_text(
                json.dumps(
                    {
                        "absent_member": name,
                        "deleted_uid": previous_uid,
                        "previous_leader": leader,
                        "elected_leader": elected,
                        "topology_id": topology_id,
                        "members": {member: rehearsal.master_json(member, "cluster/status") for member in survivors},
                    },
                    indent=2,
                )
            )
            rehearsal.archive(f"{phase}-absent")
        admitted_members[name] = rehearsal.create(pod_manifest)
        (undeclared_outputs_dir() / f"{phase}-created-pod.json").write_text(
            json.dumps(admitted_members[name], indent=2)
        )
        rehearsal.kubectl("wait", f"pod/{name}", "--for=condition=Ready", "--timeout=180s")
        pod = rehearsal.get("pod", name)
        assert pod["metadata"]["uid"] != previous_uid
        claims = [
            volume["persistentVolumeClaim"]["claimName"]
            for volume in pod["spec"]["volumes"]
            if "persistentVolumeClaim" in volume
        ]
        assert claims == [f"rehearsal-master-{name}"]
        wait_master_agreement(names)
        log = raft_files(name, phase)
        rehearsal.archive(phase)
        verify_s3(phase)
        return log

    for ordinal, follower in enumerate(name for name in names if name != old_leader):
        if wait_master_agreement(names) == follower:
            pytest.fail(
                f"Next original member {follower} became leader before its initial conversion; review the rolling sequence"
            )
        before_log = replace_master(follower, f"follower-{ordinal}-persistent")
        claim_name = f"rehearsal-master-{follower}"
        claim = rehearsal.get("pvc", claim_name)
        claim_uid = claim["metadata"]["uid"]
        pv_uid = rehearsal.get("pv", claim["spec"]["volumeName"])["metadata"]["uid"]
        after_log = replace_master(follower, f"follower-{ordinal}-recovered")
        assert after_log.startswith(before_log)
        assert rehearsal.get("pvc", claim_name)["metadata"]["uid"] == claim_uid
        assert rehearsal.get("pv", claim["spec"]["volumeName"])["metadata"]["uid"] == pv_uid
        logs = rehearsal.kubectl("logs", follower, "-c", "master")
        assert any(
            message + topology_id in logs
            for message in (
                "Recovered TopologyId: ",
                "Recovered TopologyId from snapshot: ",
                "TopologyId applied on follower: ",
                "TopologyId generated and applied on leader: ",
            )
        )

    # Leadership can change during a restart. Keep the original ephemeral
    # leader last while proving the two persistent members can serve without it.
    replace_master(old_leader, "old-leader-last", hold_out_until_quorum=True)
    current_leader = wait_master_agreement(names)
    leader_claim = rehearsal.get("pvc", f"rehearsal-master-{current_leader}")
    leader_pv_uid = rehearsal.get("pv", leader_claim["spec"]["volumeName"])["metadata"]["uid"]
    before_leader_log = raft_files(current_leader, "before-all-persistent-leader-failover")
    after_leader_log = replace_master(current_leader, "all-persistent-leader-failover", hold_out_until_quorum=True)
    assert after_leader_log.startswith(before_leader_log)
    assert rehearsal.get("pvc", leader_claim["metadata"]["name"])["metadata"]["uid"] == leader_claim["metadata"]["uid"]
    assert rehearsal.get("pv", leader_claim["spec"]["volumeName"])["metadata"]["uid"] == leader_pv_uid
    statuses = {name: rehearsal.master_json(name, "cluster/status") for name in names}
    assert len({status["Leader"] for status in statuses.values()}) == 1
    assert sum(bool(status.get("IsLeader")) for status in statuses.values()) == 1
    assert all(rehearsal.master_json(name, "dir/status")["TopologyId"] == topology_id for name in names)
    final_claims = [
        item for item in rehearsal.get("pvc")["items"] if item["metadata"]["name"].startswith("rehearsal-master-")
    ]
    assert len(final_claims) == 3
    assert len({claim["spec"]["volumeName"] for claim in final_claims}) == 3
    verify_s3("final")
    final_uids = {name: rehearsal.get("pod", name)["metadata"]["uid"] for name in names}
    assert all(final_uids[name] != original_uids[name] for name in names)
    admitted = rehearsal.create(replacement)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if all(
            any(
                owner.get("controller") and owner["uid"] == admitted["metadata"]["uid"]
                for owner in rehearsal.get("pod", name)["metadata"].get("ownerReferences", [])
            )
            for name in names
        ):
            break
        time.sleep(1)
    else:
        pytest.fail("Final StatefulSet did not adopt each already-persistent master Pod")
    assert {name: rehearsal.get("pod", name)["metadata"]["uid"] for name in names} == final_uids
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        observed = rehearsal.get("statefulset", "rehearsal-master")
        if observed.get("status", {}).get("observedGeneration") == observed["metadata"]["generation"] and observed.get(
            "status", {}
        ).get("updateRevision"):
            revision = observed["status"]["updateRevision"]
            break
        time.sleep(1)
    else:
        pytest.fail("Adopting StatefulSet did not publish its current generation and update revision")
    for name in names:
        current = rehearsal.get("pod", name)
        created = admitted_members[name]
        assert current["metadata"]["uid"] == created["metadata"]["uid"]
        assert {key: value for key, value in current["spec"].items() if key != "nodeName"} == {
            key: value for key, value in created["spec"].items() if key != "nodeName"
        }
        assert_manifest_fields(member_manifests[name][1]["spec"], current["spec"])
        assert {container["name"] for container in current["spec"]["containers"]} == {
            container["name"] for container in member_manifests[name][1]["spec"]["containers"]
        }
        rehearsal.patch("pod", name, {"metadata": {"labels": {"controller-revision-hash": revision}}})
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if rehearsal.get("statefulset", "rehearsal-master").get("status", {}).get("updatedReplicas") == 3:
            break
        time.sleep(1)
    else:
        pytest.fail("StatefulSet did not recognize all three verified persistent Pods at its current revision")
    rehearsal.kubectl("scale", "deployment/operator", "--replicas=1")
    rehearsal.kubectl("rollout", "status", "deployment/operator", "--timeout=120s")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if "ensure master stateful set rehearsal-master" in rehearsal.kubectl("logs", "deployment/operator"):
            break
        time.sleep(2)
    else:
        pytest.fail("Operator did not reconcile the final adopted StatefulSet after resuming")
    reconciled = rehearsal.get("statefulset", "rehearsal-master")
    assert reconciled["spec"]["template"] == admitted["spec"]["template"]
    assert reconciled["spec"]["updateStrategy"] == {"type": "OnDelete"}
    assert {name: rehearsal.get("pod", name)["metadata"]["uid"] for name in names} == final_uids
    assert rehearsal.get("service", "rehearsal-filer")["spec"]["clusterIP"] == "None"
    rehearsal.archive("adopted-persistent-ondelete")
    verify_s3("adopted-persistent")
    rehearsal.patch(
        "statefulset",
        "rehearsal-master",
        {"spec": {"updateStrategy": {"type": "RollingUpdate", "rollingUpdate": {"partition": 0}}}},
    )
    rehearsal.kubectl("rollout", "status", "statefulset/rehearsal-master", "--timeout=60s")
    assert {name: rehearsal.get("pod", name)["metadata"]["uid"] for name in names} == final_uids
    rehearsal.archive("rollingupdate-restored")


if __name__ == "__main__":
    pytest_bazel.main()
