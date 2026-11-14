"""Clean certification follows workload removal and successful owned-resource release."""

import contextlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.deployment import rendered_fixture, stack
from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.k3s.namespaces import CREDENTIALS_NAMESPACE, NAMESPACE
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace


@pytest.fixture
def fixture() -> rendered_fixture.RenderedFixture:
    return rendered_fixture.RenderedFixture(
        backend=rendered_fixture.Backend.LOCAL_MODEL,
        vm_sandboxes=False,
        namespace=NAMESPACE,
        app_origin="http://app.test.invalid",
        browser_services=[],
        mcp_fixtures=None,
        objects=[
            {"kind": "Namespace", "metadata": {"name": name}}
            for name in (NAMESPACE, CREDENTIALS_NAMESPACE, kubernetes_accounts_namespace(NAMESPACE))
        ],
    )


def test_namespace_cleanup_snapshot_requests_metadata_only_fields() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    class FakeK3s:
        def kubectl(self, *args: str, **kwargs: Any) -> str:
            calls.append((args, kwargs))
            if args[:2] == ("get", "namespace"):
                return (
                    "NAME UID FINALIZERS PHASE CONDITIONS\n"
                    "systest namespace-uid controller Terminating SomeResourcesRemain\n"
                )
            if args[:2] == ("get", "-n"):
                return (
                    "KIND NAME UID DELETING FINALIZERS OWNERS PHASE VOLUME CONTAINERS GRACE\n"
                    "Pod app-0 pod-uid 2026-11-14T17:54:10Z example.test/cleanup Deployment/app Running app 30\n"
                    "PersistentVolumeClaim state-claim pvc-uid 2026-11-14T17:54:10Z "
                    "kubernetes Pod/app Pending pv-state <none> <none>\n"
                )
            raise AssertionError(f"unexpected snapshot query: {args}")

    snapshot = stack._capture_namespace_cleanup_snapshot(FakeK3s(), ["systest"], threading.Event())  # type: ignore[arg-type]
    encoded = json.dumps(snapshot)

    assert "systest" in encoded
    assert "pod-uid" in encoded
    assert snapshot["namespaces"]["systest"]["state_error"] is None
    assert "controller Terminating SomeResourcesRemain" in snapshot["namespaces"]["systest"]["state"]
    assert snapshot["namespaces"]["systest"]["objects_error"] is None
    assert "example.test/cleanup Deployment/app Running" in snapshot["namespaces"]["systest"]["pods_and_pvcs"]
    assert "PersistentVolumeClaim state-claim pvc-uid" in snapshot["namespaces"]["systest"]["pods_and_pvcs"]
    for args, kwargs in calls:
        columns = next(argument for argument in args if argument.startswith("-o=custom-columns="))
        assert kwargs == {"check": True, "timeout": 2, "stdout_only": True}
        assert "--request-timeout=2s" in args
        if args[:2] == ("get", "namespace"):
            assert ".spec.finalizers" in columns
            assert ".status.conditions[*].message" in columns
        else:
            assert args[3] == "pods,persistentvolumeclaims"
            assert ".metadata.finalizers" in columns
            assert ".metadata.ownerReferences" in columns
            assert ".status.containerStatuses" in columns
            assert ".spec.terminationGracePeriodSeconds" in columns
            assert ".spec.nodeName" in columns
            assert ".spec.volumeName" in columns
        assert not any(
            forbidden in columns
            for forbidden in (".metadata.annotations", ".data", ".stringData", ".spec.containers", ".env")
        )


def test_namespace_cleanup_observer_failure_does_not_mask_delete_error(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_error = RuntimeError("namespace deletion failed")
    calls: list[tuple[str, ...]] = []

    class FakeK3s:
        def kubectl(self, *args: str, **_kwargs: Any) -> str:
            calls.append(args)
            raise delete_error

    def fail_observer(*_args: Any) -> None:
        raise ValueError("diagnostic failed")

    monkeypatch.setattr(stack, "_observe_namespace_cleanup", fail_observer)
    with pytest.raises(RuntimeError, match="namespace deletion failed") as raised:
        stack._delete_fixture_namespaces(FakeK3s(), ["systest", "credentials", "accounts"])  # type: ignore[arg-type]

    assert raised.value is delete_error
    assert calls == [("delete", "namespace", "systest", "credentials", "accounts", "--wait=true", "--timeout=60s")]


@pytest.mark.parametrize("failure", [None, "resources", "workloads", "volumes"])
def test_clean_marker_requires_every_cleanup_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: rendered_fixture.RenderedFixture, failure: str | None
) -> None:
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", tmp_path / "server.json")
    marker: dict[str, Any] = {"state": "dirty", "fingerprint": "test-infra", "identity": {}}
    baseline.write_marker(marker)
    order = []

    def close(stage: str) -> None:
        # Until every boundary succeeds, a subsequent action must reject reuse.
        assert json.loads((tmp_path / "server.json").read_text())["state"] == "dirty"
        order.append(stage)
        if failure == stage:
            raise RuntimeError("owned resource did not close")

    monkeypatch.setattr(stack, "_baseline_clean", lambda _node, _fixture: close("workloads"))
    resources, volumes = contextlib.ExitStack(), contextlib.ExitStack()
    resources.callback(close, "resources")
    volumes.callback(close, "volumes")
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    if failure is None:
        assert stack._certify_baseline(node, fixture, None, marker, resources, volumes) == 0
        assert order == ["resources", "workloads", "volumes"]
        assert json.loads((tmp_path / "server.json").read_text())["state"] == "clean"
    else:
        with pytest.raises(RuntimeError, match="owned resource did not close"):
            stack._certify_baseline(node, fixture, None, marker, resources, volumes)
        assert json.loads((tmp_path / "server.json").read_text())["state"] == "dirty"
        assert order[-1] == failure


def test_leftover_sandbox_prevents_workload_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: rendered_fixture.RenderedFixture
) -> None:
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    calls = []

    def kubectl(*args: str) -> str:
        calls.append(args)
        return json.dumps({"items": [{"metadata": {"name": "test-leftover-sandbox"}}]})

    monkeypatch.setattr(node, "kubectl", kubectl)
    with pytest.raises(RuntimeError, match="Sandboxes"):
        stack._baseline_clean(node, fixture)
    assert all(command[0] == "get" for command in calls)


@pytest.mark.parametrize("existing", ["marker", "private_data", "missing_test_tmpdir", "relative_test_tmpdir"])
def test_fresh_kvm_refusal_precedes_stack_creation_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    root = tmp_path / "retained"
    root.mkdir()
    marker = root / "server.json"
    data_parent = tmp_path / "action-test-tmp"
    data_parent.mkdir()
    data_dir = data_parent / "k3s"
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", root)
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", marker)
    if existing == "marker":
        marker.write_text('{"state":"dirty"}')
    elif existing == "private_data":
        data_dir.mkdir()
    monkeypatch.setenv("AGENTPLANE_K3S_REQUIRE_KVM", "1")
    monkeypatch.delenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", raising=False)
    if existing == "missing_test_tmpdir":
        monkeypatch.delenv("TEST_TMPDIR", raising=False)
    elif existing == "relative_test_tmpdir":
        monkeypatch.setenv("TEST_TMPDIR", "relative-test-tmp")
    else:
        monkeypatch.setenv("TEST_TMPDIR", str(data_parent))

    calls: list[str] = []
    monkeypatch.setattr(stack.baseline, "stop_previous", lambda: calls.append("stop_previous"))
    monkeypatch.setattr(stack.baseline, "stop_owned", lambda *_args: calls.append("stop_owned"))
    monkeypatch.setattr(stack.baseline, "_reset_previous_stack", lambda *_args: calls.append("reset"))
    monkeypatch.setattr(stack.tempfile, "mkdtemp", lambda: calls.append("work_mkdir"))
    monkeypatch.setattr(stack, "K3s", lambda *_args, **_kwargs: calls.append("construct_k3s"))

    expected_error = (
        "TEST_TMPDIR" if existing in {"missing_test_tmpdir", "relative_test_tmpdir"} else "fresh KVM action found"
    )
    with (
        pytest.raises(RuntimeError, match=expected_error),
        stack.running_stack(
            inputs=None,  # type: ignore[arg-type]
            models=None,  # type: ignore[arg-type]
            validate_vm_shutdown=lambda: True,
            persist_infrastructure=False,
            virtualization_factory=None,
        ),
    ):
        pytest.fail("refused state must not reach a running stack")

    assert calls == []
    if existing == "marker":
        assert marker.read_text() == '{"state":"dirty"}'
    elif existing == "private_data":
        assert data_dir.is_dir()


@pytest.mark.parametrize("require_kvm", [False, True])
def test_action_store_and_previous_stop_follow_the_selected_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: rendered_fixture.RenderedFixture, require_kvm: bool
) -> None:
    root = tmp_path / "retained"
    monkeypatch.setattr(baseline, "_BASELINE_ROOT", root)
    monkeypatch.setattr(baseline, "_BASELINE_MARKER", root / "server.json")
    monkeypatch.setenv("AGENTPLANE_K3S_REQUIRE_KVM", "1" if require_kvm else "0")
    monkeypatch.delenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", raising=False)
    action_tmpdir = tmp_path / "action-test-tmp"
    if require_kvm:
        action_tmpdir.mkdir()
        monkeypatch.setenv("TEST_TMPDIR", str(action_tmpdir))
    else:
        monkeypatch.delenv("TEST_TMPDIR", raising=False)
    monkeypatch.setattr(stack.tempfile, "mkdtemp", lambda: str(tmp_path / "work"))
    monkeypatch.setattr(stack.pki_infrastructure, "releases", lambda: ([], []))
    monkeypatch.setattr(stack.process_traces, "ProcessTraces", lambda *_args: contextlib.nullcontext())
    events: list[tuple[str, dict[str, Any]]] = []

    class StartReachedError(Exception):
        """End this offline fixture test before a real node could start."""

    class FakeK3s:
        node_ip = "127.0.0.1"

        def __init__(self, work: Path, binary: Path, **kwargs: Any) -> None:
            events.append(("construct", {"work": work, "binary": binary, **kwargs}))

        def image_store_is_retainable(self) -> bool:
            return False

        def start(self, **kwargs: Any) -> None:
            events.append(("start", kwargs))
            raise StartReachedError

    monkeypatch.setattr(stack, "K3s", FakeK3s)
    monkeypatch.setattr(stack.baseline, "stop_previous", lambda: events.append(("stop_previous", {})))
    inputs = stack.StackInputs(
        fixture=fixture,
        binary=Path("k3s"),
        image_layouts={},
        pki_image_layouts={},
        vm_image_layouts={},
        guest_image_layouts={},
    )
    with (
        pytest.raises(StartReachedError),
        stack.running_stack(
            inputs,
            models=rendered_fixture.LocalModel(url="http://model.test.invalid"),
            validate_vm_shutdown=lambda: True,
            persist_infrastructure=False,
            virtualization_factory=None,
        ),
    ):
        pytest.fail("the offline start sentinel must end the fixture")

    expected_constructor = {"work": tmp_path / "work", "binary": Path("k3s")}
    if require_kvm:
        expected_constructor["data_dir"] = action_tmpdir / "k3s"
    expected: list[tuple[str, dict[str, Any]]] = [("construct", expected_constructor)]
    if not require_kvm:
        expected.append(("stop_previous", {}))
    expected.append(("start", {"layers_in_memory": True, "preserve_image_store": False, "database_in_memory": False}))
    assert events == expected
    assert not root.exists()


if __name__ == "__main__":
    pytest_bazel.main()
