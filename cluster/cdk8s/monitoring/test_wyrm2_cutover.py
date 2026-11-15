"""Filesystem handoff and failures across the external Kubernetes API boundary."""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from cluster.cdk8s.monitoring import wyrm2_cutover as cutover


class FakeApi(cutover.Api):
    def __init__(self) -> None:
        self.node: dict[str, Any] = {
            "metadata": {"name": "wyrm2", "resourceVersion": "fake-version", "labels": {"node-vendor": "nixos"}},
            "spec": {},
            "status": {
                "conditions": [
                    {"type": name, "status": status}
                    for name, status in (
                        ("Ready", "True"),
                        ("MemoryPressure", "False"),
                        ("DiskPressure", "False"),
                        ("PIDPressure", "False"),
                    )
                ]
            },
        }
        self.sets: list[dict[str, Any]] = []
        for index, (namespace, name) in enumerate(cutover.COLLECTORS):
            label = cutover.POD_LABEL if index in (0, 2) else cutover.JOURNAL_LABEL
            operator, values = (
                ("In", ["alloy"]) if index == 3 else ("NotIn", ["drained"] if index == 2 else ["drained", "alloy"])
            )
            storage = (
                "/run/promtail" if index < 2 else ("/var/lib/alloy-node" if index == 2 else "/var/lib/alloy-journal")
            )
            self.sets.append(
                {
                    "metadata": {"namespace": namespace, "name": name, "uid": f"fake-ds-{index}", "generation": 1},
                    "status": {
                        "observedGeneration": 1,
                        "desiredNumberScheduled": int(index != 3),
                        "numberReady": int(index != 3),
                        "updatedNumberScheduled": int(index != 3),
                    },
                    "spec": {
                        "template": {
                            "spec": {
                                "affinity": {
                                    "nodeAffinity": {
                                        "requiredDuringSchedulingIgnoredDuringExecution": {
                                            "nodeSelectorTerms": [
                                                {
                                                    "matchExpressions": [
                                                        {"key": label, "operator": operator, "values": values}
                                                    ]
                                                }
                                            ]
                                        }
                                    }
                                },
                                "volumes": [{"name": "storage", "hostPath": {"path": storage}}],
                                "containers": [
                                    {
                                        "name": "promtail" if index < 2 else "alloy",
                                        "image": "docker.io/grafana/promtail:3.5.1"
                                        if index < 2
                                        else "docker.io/grafana/alloy:v1.22.0",
                                        "volumeMounts": [{"name": "storage", "mountPath": storage}],
                                    }
                                ],
                            }
                        }
                    },
                }
            )
            ds = self.sets[-1]
            ds["spec"]["selector"] = {"matchLabels": {"fake.example/collector": name}}
            spec = ds["spec"]["template"]["spec"]
            main = spec["containers"][0]
            config_directory = "/etc/promtail" if index < 2 else "/etc/alloy"
            source = {"secret": {"secretName": name}} if index < 2 else {"configMap": {"name": f"{name}-config"}}
            spec["volumes"].append({"name": "config", **source})
            main["volumeMounts"].append({"name": "config", "mountPath": config_directory})
            main["args"] = (
                ["-config.file=/etc/promtail/promtail.yaml"]
                if index < 2
                else [
                    "run",
                    f"/etc/alloy/{'node' if index == 2 else 'journal'}.alloy",
                    f"--storage.path={storage}",
                    "--server.http.listen-addr=0.0.0.0:12345",
                    "--server.http.ui-path-prefix=/",
                    "--stability.level=generally-available",
                ]
            )
            for path in (["/var/log/pods"] if index != 3 else []) + (
                ["/var/log/journal", "/etc/machine-id"] if index in (1, 3) else []
            ):
                spec["volumes"].append({"name": path, "hostPath": {"path": path}})
                main["volumeMounts"].append({"name": path, "mountPath": path, "readOnly": True})
        self.patches: list[str] = []
        self.drained_reads = 0
        self.fail_activation = False
        self.stopped = False

    def request(self, path: str, patch: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if patch is not None:
            assert path == cutover.NODE_API
            assert patch[0]["value"] == self.node["metadata"]["resourceVersion"]
            if patch[1]["value"][cutover.POD_LABEL] == "alloy":
                assert self.stopped
            self.node["metadata"]["labels"] = patch[1]["value"]
            self.patches.append(patch[1]["value"][cutover.POD_LABEL])
        if path == cutover.NODE_API:
            return copy.deepcopy(self.node)
        if "/daemonsets/" in path:
            return copy.deepcopy(next(ds for ds in self.sets if ds["metadata"]["name"] == path.rsplit("/", 1)[1]))
        if "/configmaps/" in path:
            return {"data": {"node.alloy": "fake configuration", "journal.alloy": "fake configuration"}}
        assert "/pods?fieldSelector=spec.nodeName%3Dwyrm2" in path
        gate = self.node["metadata"]["labels"].get(cutover.POD_LABEL)
        if gate == "alloy" and self.fail_activation:
            raise OSError("fake unavailable API after activation")
        if gate == "drained":
            self.drained_reads += 1
            if self.drained_reads > 2:
                self.stopped = True
                return {"items": []}
        items = []
        for index, ds in enumerate(self.sets):
            if ds["metadata"]["namespace"] not in path or ((index < 2) if gate == "alloy" else (index == 3)):
                continue
            items.append(
                {
                    "metadata": {
                        "name": f"fake-pod-{index}",
                        "labels": ds["spec"]["selector"]["matchLabels"],
                        "ownerReferences": [{"uid": ds["metadata"]["uid"], "controller": True}],
                    },
                    "spec": copy.deepcopy(ds["spec"]["template"]["spec"]),
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            )
        return {"items": items}


@pytest.fixture
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cutover.Files:
    monkeypatch.setenv("NODE_NAME", "wyrm2")
    monkeypatch.setattr(cutover.time, "sleep", lambda _: None)
    node, journal, promtail, evidence = (tmp_path / name for name in ("node", "journal", "promtail", "evidence"))
    for directory in (node, journal, promtail, evidence):
        directory.mkdir()
    component = node / "loki.source.file.pods"
    component.mkdir()
    (component / "positions.yml").write_bytes(b"positions: {}\n")
    (promtail / "positions.yaml").write_bytes(b"positions:\n  journal: fake-cursor\n")
    wal = node / "prometheus.remote_write.mimir"
    wal.mkdir()
    (wal / "fake-wal").write_bytes(b"preserve buffered metrics")
    return cutover.Files(
        node=node, journal=journal, promtail=promtail / "positions.yaml", pod_component=component, evidence=evidence
    )


def hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256(b"fake configuration").hexdigest()
        for name in ("alloy-node-config", "alloy-journal-config")
    }


def test_stops_writers_then_snapshots_replays_and_fences_rerun(files: cutover.Files) -> None:
    api = FakeApi()
    cutover.run(api, files, hashes())
    assert api.patches == ["drained", "alloy"]
    assert api.stopped
    attempt = files.evidence / "attempt"
    assert (attempt / "shared-promtail-positions.yaml").read_bytes() == files.promtail.read_bytes()
    assert (attempt / "unused-alloy-pod-positions.yml").read_bytes() == b"positions: {}\n"
    assert not (files.pod_component / "positions.yml").exists()
    assert (files.node / "prometheus.remote_write.mimir/fake-wal").read_bytes() == b"preserve buffered metrics"
    assert json.loads((attempt / "prepared.json").read_text())["legacy_import"] is False
    snapshots = json.loads((attempt / "snapshots.json").read_text())
    assert all(
        hashlib.sha256((attempt / name).read_bytes()).hexdigest() == digest for name, digest in snapshots.items()
    )
    (files.pod_component / "positions.yml").write_bytes(b"positions:\n  fake-active-file: 123\n")
    with pytest.raises(RuntimeError, match="prior attempt"):
        cutover.run(api, files, hashes())
    assert (files.pod_component / "positions.yml").read_bytes() == b"positions:\n  fake-active-file: 123\n"
    assert api.patches == ["drained", "alloy"]


@pytest.mark.parametrize(
    "state", ["active-pods", "legacy-import", "journal", "unknown-empty", "symlink", "drained", "config"]
)
def test_preflight_rejects_unsafe_state_without_draining(files: cutover.Files, state: str) -> None:
    api = FakeApi()
    expected = hashes()
    match state:
        case "active-pods":
            (files.pod_component / "positions.yml").write_text("positions:\n  fake-file: 123\n")
        case "legacy-import":
            (files.node / "legacy-pod-positions.yaml").write_text("positions: {}\n")
        case "journal":
            (files.journal / "loki.source.journal.journal").mkdir()
        case "unknown-empty":
            (files.pod_component / "positions.yml").write_text("# uncertain serializer\npositions: {}\n")
        case "symlink":
            (files.pod_component / "positions.yml").unlink()
            (files.pod_component / "positions.yml").symlink_to(files.promtail)
        case "drained":
            api.node["metadata"]["labels"][cutover.POD_LABEL] = "drained"
        case "config":
            expected["alloy-node-config"] = "fake-wrong-digest"
    with pytest.raises((RuntimeError, OSError)):
        cutover.run(api, files, expected)
    assert not api.patches
    assert not list(files.evidence.iterdir())
    assert files.promtail.read_bytes() == b"positions:\n  journal: fake-cursor\n"


def test_failure_after_drain_keeps_gate_and_all_original_state(
    files: cutover.Files, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeApi()
    original = cutover.wait_collectors

    def stop_then_fail(api: cutover.Api, sets: list[dict[str, Any]], *, activated: bool) -> None:
        original(api, sets, activated=activated)
        raise OSError("fake evidence disk failure")

    monkeypatch.setattr(cutover, "wait_collectors", stop_then_fail)
    with pytest.raises(OSError, match="fake evidence disk failure"):
        cutover.run(api, files, hashes())
    assert api.patches == ["drained"]
    assert (files.pod_component / "positions.yml").read_bytes() == b"positions: {}\n"
    assert (files.evidence / "attempt/preflight.json").exists()
    assert not (files.evidence / "attempt/activated.json").exists()


def test_failure_after_activation_never_rolls_back_or_clears_evidence(files: cutover.Files) -> None:
    api = FakeApi()
    api.fail_activation = True
    with pytest.raises(OSError, match="fake unavailable API"):
        cutover.run(api, files, hashes())
    assert api.patches == ["drained", "alloy"]
    assert (files.evidence / "attempt/shared-promtail-positions.yaml").read_bytes() == files.promtail.read_bytes()
    assert (files.evidence / "attempt/activated.json").exists()
    assert not (files.evidence / "attempt/ready.json").exists()


def test_concurrent_gate_change_is_not_overwritten(files: cutover.Files) -> None:
    class RacingApi(FakeApi):
        def request(self, path: str, patch: list[dict[str, Any]] | None = None) -> dict[str, Any]:
            if path == cutover.NODE_API and patch is None and self.stopped:
                self.node["metadata"]["labels"][cutover.POD_LABEL] = "promtail"
            return super().request(path, patch)

    api = RacingApi()
    with pytest.raises(RuntimeError, match="gates changed concurrently"):
        cutover.run(api, files, hashes())
    assert api.patches == ["drained"]
    assert api.node["metadata"]["labels"][cutover.POD_LABEL] == "promtail"
    assert (files.evidence / "attempt/shared-promtail-positions.yaml").read_bytes() == files.promtail.read_bytes()
    assert not (files.evidence / "attempt/activated.json").exists()


if __name__ == "__main__":
    pytest_bazel.main()
