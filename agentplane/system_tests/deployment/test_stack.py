"""Clean certification follows workload removal and successful owned-resource release."""

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentplane.system_tests.deployment import rendered_fixture, stack
from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.k3s.namespaces import CREDENTIALS_NAMESPACE, NAMESPACE
from cluster.cdk8s.agentplane.namespaces import kubernetes_accounts_namespace


@pytest.fixture
def fixture() -> rendered_fixture.RenderedFixture:
    return rendered_fixture.RenderedFixture(
        backend=rendered_fixture.Backend.LOCAL_MODEL,
        runtime=rendered_fixture.SandboxRuntime.POD,
        namespace=NAMESPACE,
        app_origin="http://app.test.invalid",
        browser_services=[],
        mcp_fixtures=None,
        objects=[
            {"kind": "Namespace", "metadata": {"name": name}}
            for name in (NAMESPACE, CREDENTIALS_NAMESPACE, kubernetes_accounts_namespace(NAMESPACE))
        ],
    )


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
        assert stack._certify_baseline(node, fixture, None, None, marker, resources, volumes) == 0
        assert order == ["resources", "workloads", "volumes"]
        assert json.loads((tmp_path / "server.json").read_text())["state"] == "clean"
    else:
        with pytest.raises(RuntimeError, match="owned resource did not close"):
            stack._certify_baseline(node, fixture, None, None, marker, resources, volumes)
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


@pytest.mark.parametrize("stop_fails", [False, True])
@pytest.mark.parametrize("failure_stage", ["action", "resources", "workloads"])
def test_stack_failure_stops_owned_baseline_before_loop_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: rendered_fixture.RenderedFixture,
    failure_stage: str,
    stop_fails: bool,
) -> None:
    """A failed retained stack must drop its pod owners before raw fixture volumes unwind."""
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    marker: dict[str, Any] = {"state": "dirty", "identity": {"pid": 123}}
    events: list[str] = []
    stopped: list[tuple[baseline.K3s, dict[str, Any]]] = []

    def stop_owned(actual_node: baseline.K3s, actual_marker: dict[str, Any]) -> None:
        events.append("stop owned server and pods")
        stopped.append((actual_node, actual_marker))
        if stop_fails:
            raise RuntimeError("owned server stop failed")

    monkeypatch.setattr(baseline, "stop_owned", stop_owned)

    @contextlib.contextmanager
    def loop_volume():
        try:
            yield
        finally:
            events.append("detach fixture loop")

    @contextlib.contextmanager
    def failing_resource():
        try:
            yield
        finally:
            events.append("close stack resource")
            if failure_stage == "resources":
                raise RuntimeError("resource close failed")

    def fail_workload_cleanup(_node: baseline.K3s, _fixture: rendered_fixture.RenderedFixture) -> None:
        events.append("check leftover Sandboxes")
        if failure_stage == "workloads":
            raise RuntimeError("fixture left 1 Sandboxes")

    monkeypatch.setattr(stack, "_baseline_clean", fail_workload_cleanup)

    def fail_baseline_cleanup() -> None:
        # running_stack exits these stacks in this order: resources, retained baseline, volumes.
        with contextlib.ExitStack() as volumes, contextlib.ExitStack() as retained, contextlib.ExitStack() as resources:
            volumes.enter_context(loop_volume())
            retained.enter_context(stack._stop_retained_baseline_on_error(node, marker))
            resources.enter_context(failing_resource())
            if failure_stage == "action":
                raise RuntimeError("SandboxNotReadyError")
            stack._certify_baseline(node, fixture, None, None, marker, resources, volumes)

    expected_error = {
        "action": "SandboxNotReadyError",
        "resources": "resource close failed",
        "workloads": "fixture left 1 Sandboxes",
    }[failure_stage]
    with pytest.raises(RuntimeError, match=expected_error) as raised:
        fail_baseline_cleanup()

    expected_events = {
        "action": ["close stack resource"],
        "resources": ["close stack resource"],
        "workloads": ["close stack resource", "check leftover Sandboxes"],
    }[failure_stage]
    expected_events += ["stop owned server and pods", "detach fixture loop"]
    assert events == expected_events
    assert stopped == [(node, marker)]
    assert marker["state"] == "dirty"
    if stop_fails:
        assert any("owned server stop failed" in note for note in raised.value.__notes__)


def test_successful_retained_stack_does_not_stop_its_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    marker: dict[str, Any] = {"state": "clean", "identity": {"pid": 123}}
    stopped = False

    def stop_owned(_node: baseline.K3s, _marker: dict[str, Any]) -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(baseline, "stop_owned", stop_owned)
    with stack._stop_retained_baseline_on_error(node, marker):
        pass
    assert not stopped


@pytest.mark.parametrize(("value", "expected"), [(None, False), ("0", False), ("1", True)])
def test_image_import_profile_setting_is_strict_boolean(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv(stack.IMAGE_IMPORT_PROFILES_ENV, raising=False)
    else:
        monkeypatch.setenv(stack.IMAGE_IMPORT_PROFILES_ENV, value)
    assert stack._image_import_profiles_enabled() is expected


@pytest.mark.parametrize("value", ["", "true", "yes", "2"])
def test_image_import_profile_setting_rejects_non_boolean_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(stack.IMAGE_IMPORT_PROFILES_ENV, value)
    with pytest.raises(ValueError, match="must be exactly '0' or '1'"):
        stack._image_import_profiles_enabled()


@pytest.mark.parametrize("cold_baseline", [True, False])
def test_image_import_profiles_are_not_started_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cold_baseline: bool
) -> None:
    monkeypatch.delenv(stack.IMAGE_IMPORT_PROFILES_ENV, raising=False)
    monkeypatch.setattr(stack, "undeclared_outputs_dir", lambda: pytest.fail("default path requested outputs"))
    node = baseline.K3s(tmp_path, tmp_path / "unused-binary")
    monkeypatch.setattr(node, "profile_containerd", lambda *_args, **_kwargs: pytest.fail("started native profile"))

    with contextlib.ExitStack() as cleanup:
        captures = stack._start_containerd_import_profiles(
            node, cleanup, enabled=stack._image_import_profiles_enabled(), cold_baseline=cold_baseline
        )

    assert captures == []


@pytest.mark.parametrize(
    ("cold_baseline", "expected_durations"),
    [(True, {"profile": 420, "trace": 420}), (False, {"profile": 15, "trace": 5})],
)
def test_enabled_image_import_profiles_cancel_incomplete_on_short_fixture_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cold_baseline: bool, expected_durations: dict[str, int]
) -> None:
    monkeypatch.setenv(stack.IMAGE_IMPORT_PROFILES_ENV, "1")
    ready = tmp_path / "ready"
    ready.mkdir()
    binary = tmp_path / "fake-k3s"
    source = (
        f"#!{sys.executable}\n"
        "import pathlib, signal, sys, time\n"
        "kind = sys.argv[5]\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))\n"
        "sys.stdout.buffer.write(b'partial-' + kind.encode())\n"
        "sys.stdout.flush()\n"
        f"pathlib.Path({str(ready)!r}, kind).touch()\n"
        "while True: time.sleep(1)\n"
    )
    binary.write_text(source)
    binary.chmod(0o700)
    monkeypatch.setattr(baseline.Path, "is_socket", lambda _path: True)
    monkeypatch.setattr(baseline, "CONTAINERD_DEBUG_SOCKET", str(tmp_path / "containerd-debug.sock"))
    monkeypatch.setattr(stack, "undeclared_outputs_dir", lambda: tmp_path)
    node = baseline.K3s(tmp_path, binary)

    def fail_short_fixture() -> None:
        with contextlib.ExitStack() as cleanup:
            captures = stack._start_containerd_import_profiles(
                node, cleanup, enabled=stack._image_import_profiles_enabled(), cold_baseline=cold_baseline
            )
            assert len(captures) == 2
            deadline = time.monotonic() + 5
            while not all((ready / kind).exists() for kind in expected_durations):
                if time.monotonic() >= deadline:
                    pytest.fail("fake native profilers did not start")
                time.sleep(0.01)
            raise RuntimeError("fixture ended early")

    with pytest.raises(RuntimeError, match="fixture ended early"):
        fail_short_fixture()

    for kind, seconds in expected_durations.items():
        destination = tmp_path / (
            "containerd-import.cpu.pprof" if kind == "profile" else "containerd-import.runtime.trace"
        )
        metadata = json.loads(destination.with_suffix(destination.suffix + ".json").read_text())
        assert destination.read_bytes() == f"partial-{kind}".encode()
        assert metadata["requested_duration_seconds"] == seconds
        assert metadata["status"] == "cancelled"
        assert metadata["complete"] is False
