"""One-shot wyrm2 replay handoff. Deployment is the activation operation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import stat
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NODE = "wyrm2"
POD_LABEL = "observability.allegedly.works/pod-collector"
JOURNAL_LABEL = "observability.allegedly.works/journal-collector"
COLLECTORS = (
    ("loki", "promtail"),
    ("loki", "promtail-journal"),
    ("monitoring", "alloy-node"),
    ("monitoring", "alloy-journal"),
)
NODE_API = f"/api/v1/nodes/{NODE}"


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError(reason)


class Api:
    def __init__(self) -> None:
        credentials = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.token = (credentials / "token").read_text().strip()
        self.context = ssl.create_default_context(cafile=str(credentials / "ca.crt"))
        self.url = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ['KUBERNETES_SERVICE_PORT_HTTPS']}"

    def request(self, path: str, patch: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(patch).encode() if patch is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json-patch+json"},
            method="PATCH" if patch is not None else "GET",
        )
        with urllib.request.urlopen(request, context=self.context, timeout=20) as response:
            result = json.load(response)
            if not isinstance(result, dict):
                raise RuntimeError("Kubernetes response is not an object")
            return result


@dataclass(frozen=True)
class Files:
    node: Path = Path("/state/node")  # read-only, including metrics WAL
    journal: Path = Path("/state/journal")  # read-only
    pod_component: Path = Path("/state/pods")  # only writable Alloy state mount
    promtail: Path = Path("/state/promtail/positions.yaml")  # read-only shared checkpoint
    evidence: Path = Path("/evidence")

    def unused(self) -> None:
        for root, legacy in ((self.node, "legacy-pod-positions.yaml"), (self.journal, "legacy-journal-positions.yaml")):
            require(not os.path.lexists(root / legacy), f"existing import input: {root / legacy}")
        require(not list(self.journal.iterdir()), "journal storage has prior state")
        require(
            not any(
                p.name.startswith("loki.source.file.") and p.name != "loki.source.file.pods"
                for p in self.node.iterdir()
            ),
            "unexpected file-source state",
        )
        entries = list(self.pod_component.iterdir())
        require(all(p.name == "positions.yml" for p in entries), "unexpected pod component files")
        if entries:
            content = read_regular(entries[0])
            require(
                re.fullmatch(rb"\s*positions:\s*\{\}\s*", content) is not None,
                "pod positions are not demonstrably unused; never reset active state",
            )


def read_regular(path: Path) -> bytes:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
        require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), f"not a regular file: {path}")
        return source.read()


def write_once(path: Path, content: bytes) -> None:
    with path.open("xb") as destination:
        destination.write(content)
        destination.flush()
        os.fchmod(destination.fileno(), 0o400)
        os.fsync(destination.fileno())
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def record(directory: Path, name: str, value: Any) -> None:
    write_once(directory / f"{name}.json", json.dumps(value, indent=2, sort_keys=True).encode())


def healthy_node(node: dict[str, Any]) -> None:
    require(node["metadata"]["name"] == NODE, "wrong node")
    require(not node["metadata"].get("deletionTimestamp") and not node["spec"].get("unschedulable"), "node unavailable")
    require(not node["spec"].get("taints"), "unexpected wyrm2 taints")
    require(node["metadata"]["labels"].get("node-vendor") == "nixos", "not the expected NixOS node")
    conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
    require(conditions.get("Ready") == "True", "node not Ready; termination cannot be trusted")
    require(
        all(conditions.get(c) == "False" for c in ("MemoryPressure", "DiskPressure", "PIDPressure")), "node pressure"
    )


def scheduled(spec: dict[str, Any], labels: dict[str, str]) -> bool:
    require(not spec.get("nodeName") or spec["nodeName"] == NODE, "unexpected fixed node")
    if any(labels.get(k) != v for k, v in spec.get("nodeSelector", {}).items()):
        return False
    terms = (
        spec.get("affinity", {})
        .get("nodeAffinity", {})
        .get("requiredDuringSchedulingIgnoredDuringExecution", {})
        .get("nodeSelectorTerms")
    )
    if terms is None:
        return True
    matches = []
    for term in terms:
        require(not term.get("matchFields"), "unsupported collector matchFields")
        expressions = term.get("matchExpressions", [])
        matched = bool(expressions)
        for expression in expressions:
            key, operator = expression["key"], expression["operator"]
            values = expression.get("values", [])
            require(operator in {"In", "NotIn", "Exists", "DoesNotExist"}, "unsupported affinity operator")
            matched &= {
                "In": key in labels and labels[key] in values,
                "NotIn": labels.get(key) not in values,
                "Exists": key in labels,
                "DoesNotExist": key not in labels,
            }[operator]
        matches.append(matched)
    return any(matches)


def collectors(api: Api) -> list[dict[str, Any]]:
    return [api.request(f"/apis/apps/v1/namespaces/{ns}/daemonsets/{name}") for ns, name in COLLECTORS]


def pods(api: Api) -> list[dict[str, Any]]:
    return [
        pod
        for namespace in ("loki", "monitoring")
        for pod in api.request(f"/api/v1/namespaces/{namespace}/pods?fieldSelector=spec.nodeName%3D{NODE}")["items"]
    ]


def owned(pod: dict[str, Any], daemonset: dict[str, Any]) -> bool:
    return any(
        owner.get("controller") and owner["uid"] == daemonset["metadata"]["uid"]
        for owner in pod["metadata"].get("ownerReferences", [])
    )


def matches(pod: dict[str, Any], daemonset: dict[str, Any]) -> bool:
    selector = daemonset["spec"]["selector"]
    require(bool(selector.get("matchLabels")) and not selector.get("matchExpressions"), "unexpected collector selector")
    return owned(pod, daemonset) or all(
        pod["metadata"].get("labels", {}).get(k) == v for k, v in selector["matchLabels"].items()
    )


def ready(pod: dict[str, Any]) -> bool:
    return not pod["metadata"].get("deletionTimestamp") and any(
        c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", [])
    )


def reviewed_template(spec: dict[str, Any], index: int) -> dict[str, Any]:
    main_name = "promtail" if index < 2 else "alloy"
    main: dict[str, Any] = next(c for c in spec["containers"] if c["name"] == main_name)
    expected_image = "docker.io/grafana/promtail:3.5.1" if index < 2 else "docker.io/grafana/alloy:v1.22.0"
    require(main["image"] == expected_image and not main.get("command"), "unreviewed image/entrypoint")
    volumes = {v["name"]: v for v in spec["volumes"]}
    mounts = {m["mountPath"]: m for m in main["volumeMounts"]}
    config_directory = "/etc/promtail" if index < 2 else "/etc/alloy"
    require(mounts.get(config_directory) == {"name": "config", "mountPath": config_directory}, "config mount drift")
    source = volumes["config"].get("secret" if index < 2 else "configMap", {})
    require(not source.get("items"), "config key remapping is not reviewed")
    if index < 2:
        require(source.get("secretName") == COLLECTORS[index][1], "wrong Promtail Secret name")
        expected_args = ["-config.file=/etc/promtail/promtail.yaml"]
        storage = "/run/promtail"
    else:
        component = "node" if index == 2 else "journal"
        require(source.get("name") == f"alloy-{component}-config", "wrong Alloy ConfigMap")
        storage = f"/var/lib/alloy-{component}"
        expected_args = [
            "run",
            f"/etc/alloy/{component}.alloy",
            f"--storage.path={storage}",
            "--server.http.listen-addr=0.0.0.0:12345",
            "--server.http.ui-path-prefix=/",
            "--stability.level=generally-available",
        ]
    require(main.get("args") == expected_args, "collector config/storage args drift")
    required_paths = {storage: False}
    if index in (0, 1, 2):
        required_paths["/var/log/pods"] = True
    if index in (1, 3):
        required_paths |= {"/var/log/journal": True, "/etc/machine-id": True}
    for path, read_only in required_paths.items():
        mount = mounts.get(path, {})
        require(
            bool(mount)
            and not mount.get("subPath")
            and not mount.get("subPathExpr")
            and bool(mount.get("readOnly")) == read_only,
            f"collector mount drift: {path}",
        )
        require(volumes[mount["name"]].get("hostPath", {}).get("path") == path, f"wrong host path: {path}")
    return main


def check_configs(api: Api, expected: dict[str, str]) -> None:
    require(set(expected) == {"alloy-node-config", "alloy-journal-config"}, "incomplete expected configuration")
    for name, key in (("alloy-node-config", "node.alloy"), ("alloy-journal-config", "journal.alloy")):
        config = api.request(f"/api/v1/namespaces/monitoring/configmaps/{name}")
        require(hashlib.sha256(config["data"][key].encode()).hexdigest() == expected[name], "Alloy config mismatch")


def check_collectors(sets: list[dict[str, Any]], node: dict[str, Any], current_pods: list[dict[str, Any]]) -> None:
    for index, ds in enumerate(sets):
        metadata, status, template = ds["metadata"], ds["status"], ds["spec"]["template"]["spec"]
        require(status.get("observedGeneration", 0) >= metadata["generation"], "DaemonSet not observed")
        desired = status["desiredNumberScheduled"]
        require(
            status.get("numberReady", 0) == desired
            and status.get("updatedNumberScheduled", 0) == desired
            and not status.get("numberMisscheduled", 0),
            f"DaemonSet rollout incomplete: {metadata['name']}",
        )
        for gate, expected in (("promtail", index != 3), ("drained", False), ("alloy", index >= 2)):
            labels = node["metadata"]["labels"] | {POD_LABEL: gate, JOURNAL_LABEL: gate}
            require(scheduled(template, labels) == expected, f"wrong {gate} scheduling gate: {metadata['name']}")
        main = reviewed_template(template, index)
        on_node = [p for p in current_pods if matches(p, ds)]
        require(len(on_node) == (0 if index == 3 else 1), f"unexpected collectors on wyrm2: {metadata['name']}")
        for pod in on_node:
            require(owned(pod, ds), "unowned collector Pod")
            require(ready(pod), f"collector not Ready: {pod['metadata']['name']}")
            actual = reviewed_template(pod["spec"], index)
            require(
                all(actual.get(key) == main.get(key) for key in ("image", "args", "command", "env")),
                "pod differs from reviewed template",
            )
            require(all(mount in actual["volumeMounts"] for mount in main["volumeMounts"]), "pod mounts differ")


def set_gates(api: Api, previous: tuple[str | None, str | None], value: str) -> dict[str, Any]:
    node = api.request(NODE_API)
    healthy_node(node)
    labels = node["metadata"]["labels"]
    require(
        tuple(labels.get(k) for k in (POD_LABEL, JOURNAL_LABEL)) == previous, "collector gates changed concurrently"
    )
    return api.request(
        NODE_API,
        [
            {"op": "test", "path": "/metadata/resourceVersion", "value": node["metadata"]["resourceVersion"]},
            {"op": "add", "path": "/metadata/labels", "value": labels | {POD_LABEL: value, JOURNAL_LABEL: value}},
        ],
    )


def wait_collectors(api: Api, sets: list[dict[str, Any]], *, activated: bool) -> None:
    deadline = time.monotonic() + 300
    while True:
        node = api.request(NODE_API)
        healthy_node(node)
        expected = "alloy" if activated else "drained"
        require(all(node["metadata"]["labels"].get(k) == expected for k in (POD_LABEL, JOURNAL_LABEL)), "gates changed")
        current = pods(api)
        present = [[p for p in current if matches(p, ds)] for ds in sets]
        if not activated and not any(present):
            return
        if (
            activated
            and not any(present[:2])
            and all(
                len(p) == 1 and ready(p[0]) and owned(p[0], ds) for p, ds in zip(present[2:], sets[2:], strict=True)
            )
        ):
            return
        require(time.monotonic() < deadline, f"collector {'startup' if activated else 'termination'} timed out")
        time.sleep(2)


def run(api: Api, files: Files, expected_configs: dict[str, str]) -> None:
    require(os.environ.get("NODE_NAME") == NODE, "Job did not start on wyrm2")
    require(not list(files.evidence.iterdir()), "prior attempt exists; inspect evidence, never automatically rerun")
    node = api.request(NODE_API)
    healthy_node(node)
    previous = (node["metadata"]["labels"].get(POD_LABEL), node["metadata"]["labels"].get(JOURNAL_LABEL))
    require(all(value in (None, "promtail") for value in previous), "node already drained/activated or unknown gates")
    sets, current_pods = collectors(api), pods(api)
    check_collectors(sets, node, current_pods)
    check_configs(api, expected_configs)
    files.unused()
    require(bool(read_regular(files.promtail)), "no legacy rollback checkpoint")
    attempt = files.evidence / "attempt"
    attempt.mkdir(mode=0o700)  # exclusive durable fence: every later invocation refuses
    sync_directory(files.evidence)
    record(
        attempt,
        "preflight",
        {
            "time": datetime.now(UTC).isoformat(),
            "node": node,
            "daemonsets": sets,
            "pods": [p for p in current_pods if any(matches(p, ds) for ds in sets)],
            "mode": "retained-file-and-12h-journal-replay",
        },
    )
    set_gates(api, previous, "drained")
    record(attempt, "drained", {"time": datetime.now(UTC).isoformat()})
    wait_collectors(api, sets, activated=False)
    require(
        all(
            before["spec"] == after["spec"] and before["metadata"]["uid"] == after["metadata"]["uid"]
            for before, after in zip(sets, collectors(api), strict=True)
        ),
        "DaemonSet changed during drain",
    )
    files.unused()
    legacy = read_regular(files.promtail)
    require(bool(legacy), "stopped rollback checkpoint missing")
    write_once(attempt / "shared-promtail-positions.yaml", legacy)
    checksums = {"shared-promtail-positions.yaml": hashlib.sha256(legacy).hexdigest()}
    positions = files.pod_component / "positions.yml"
    if positions.exists():
        content = read_regular(positions)
        require(re.fullmatch(rb"\s*positions:\s*\{\}\s*", content) is not None, "pod state changed after drain")
        write_once(attempt / "unused-alloy-pod-positions.yml", content)
        checksums["unused-alloy-pod-positions.yml"] = hashlib.sha256(content).hexdigest()
        positions.unlink()  # only demonstrably unused state, with all writers stopped
        sync_directory(files.pod_component)
    record(attempt, "snapshots", checksums)
    record(attempt, "prepared", {"time": datetime.now(UTC).isoformat(), "legacy_import": False})
    check_configs(api, expected_configs)
    set_gates(api, ("drained", "drained"), "alloy")
    record(attempt, "activated", {"time": datetime.now(UTC).isoformat()})
    wait_collectors(api, sets, activated=True)
    record(
        attempt,
        "ready",
        {"time": datetime.now(UTC).isoformat(), "pods": [p for p in pods(api) if any(matches(p, ds) for ds in sets)]},
    )
    print(
        "wyrm2 Alloy ready; operator must verify Loki canaries, replay, rotation, restart and metrics recovery",
        flush=True,
    )


if __name__ == "__main__":
    try:
        run(Api(), Files(), json.loads(Path("/scripts/config-hashes.json").read_text()))
    except Exception as error:
        print(
            f"STOP: {error}. No automatic rollback. Inspect Node/wyrm2 labels and /evidence/attempt; "
            "if drained, keep it drained while investigating; if alloy, preserve active state. "
            "See docs/wyrm2-alloy-canary.md for recovery.",
            file=sys.stderr,
            flush=True,
        )
        raise
