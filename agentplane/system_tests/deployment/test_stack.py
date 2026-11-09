"""Clean certification follows workload removal and successful owned-resource release."""

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_bazel

from agentplane.system_tests.deployment import rendered_fixture, stack
from agentplane.system_tests.k3s import baseline
from agentplane.system_tests.k3s.namespaces import ACCOUNTS_NAMESPACE, CREDENTIALS_NAMESPACE, NAMESPACE


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
            for name in (NAMESPACE, CREDENTIALS_NAMESPACE, ACCOUNTS_NAMESPACE)
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


if __name__ == "__main__":
    pytest_bazel.main()
