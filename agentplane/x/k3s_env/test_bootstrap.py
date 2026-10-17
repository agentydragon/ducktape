import subprocess
from unittest.mock import Mock

import pytest
import pytest_bazel

from agentplane.x.k3s_env import bootstrap
from agentplane.x.k3s_env.bootstrap import Bootstrap


def test_waits_for_ready_after_transient_api_failure(starting_k3s: Bootstrap, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 1, "", "connection refused"),
            subprocess.CompletedProcess([], 0, "False", ""),
            subprocess.CompletedProcess([], 0, "True", ""),
        ]
    )
    monkeypatch.setattr(bootstrap.subprocess, "run", probe)

    starting_k3s.wait_for("get", "node/env", expected="True")

    assert probe.call_count == 3


def test_server_exit_before_kubeconfig_reports_log(starting_k3s: Bootstrap, k3s_process: Mock) -> None:
    starting_k3s.kubeconfig.unlink()
    k3s_process.poll.return_value = 17

    with pytest.raises(RuntimeError, match="k3s exited with 17") as error:
        starting_k3s.wait_for("get", "node/env", expected="True")

    assert "fixture server diagnostic" in str(error.value)


def test_missing_kubeconfig_has_a_deadline(starting_k3s: Bootstrap, monkeypatch: pytest.MonkeyPatch) -> None:
    starting_k3s.kubeconfig.unlink()
    probe = Mock()
    monkeypatch.setattr(bootstrap.subprocess, "run", probe)

    with pytest.raises(RuntimeError, match="bootstrap deadline expired"):
        starting_k3s.wait_for("get", "node/env", expected="True")

    probe.assert_not_called()


def test_unresponsive_api_probes_have_a_timeout(starting_k3s: Bootstrap, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Mock(side_effect=subprocess.TimeoutExpired("kubectl", 1))
    monkeypatch.setattr(bootstrap.subprocess, "run", probe)

    with pytest.raises(RuntimeError, match="kubectl probe timed out"):
        starting_k3s.wait_for("get", "deploy/coredns", expected="deployment.apps/coredns")

    assert [call.kwargs["timeout"] for call in probe.call_args_list] == [3, 2, 1]


def test_server_exit_during_api_probe_is_not_readiness(
    starting_k3s: Bootstrap, k3s_process: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def exit_during_probe(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        k3s_process.poll.return_value = 23
        return subprocess.CompletedProcess([], 0, "True", "")

    monkeypatch.setattr(bootstrap.subprocess, "run", exit_during_probe)

    with pytest.raises(RuntimeError, match="k3s exited with 23"):
        starting_k3s.wait_for("get", "node/env", expected="True")


if __name__ == "__main__":
    pytest_bazel.main()
