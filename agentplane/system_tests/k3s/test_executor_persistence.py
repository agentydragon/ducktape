"""Retention requires the permission injected through BuildBuddy's exec properties."""

from pathlib import Path

import pytest

from agentplane.system_tests.k3s import stack


@pytest.mark.parametrize("permission", [None, "", "0", "true"])
def test_no_retention_without_executor_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, permission: str | None
) -> None:
    # Credentials and generic CI context are also present on developer machines and runners.
    monkeypatch.setenv("BUILDBUDDY_API_KEY", "test-key")
    monkeypatch.setenv("CI", "true")
    if permission is None:
        monkeypatch.delenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", raising=False)
    else:
        monkeypatch.setenv("AGENTPLANE_K3S_EXECUTOR_PERSISTENCE", permission)
    baseline = tmp_path / "baseline"
    monkeypatch.setattr(stack, "_BASELINE_ROOT", baseline)
    with pytest.raises(RuntimeError, match="requires BuildBuddy's executor persistence permission"):
        stack._baseline_open(tmp_path / "unused-k3s", "unused-fingerprint", layers_in_memory=True)
    assert not baseline.exists()


def test_retained_daemon_logs_are_bounded_and_survive_a_missing_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "k3s.log").write_text("earlier log\nrecent log\n")
    data = tmp_path / "data"
    containerd = data / "agent/containerd"
    containerd.mkdir(parents=True)
    (containerd / "containerd.log").write_text("containerd tail\n")
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setattr(stack, "_BASELINE_ROOT", baseline)
    monkeypatch.setattr(stack, "_RETAINED_LOG_LIMIT", 11)

    stack._capture_retained_logs(data, outputs)

    assert (outputs / "k3s-retained.log").read_text() == "recent log\n"
    assert (outputs / "containerd-retained.log").read_text() == "inerd tail\n"
    assert not (outputs / "containerd-teardown-retained.log").exists()


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
