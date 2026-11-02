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


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
