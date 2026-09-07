"""The hostexec host/run_as scoping boundary: one VM, one unprivileged user, any cmd."""

from __future__ import annotations

import pytest
import pytest_bazel

from haku.console.auto_approval.decision import AutoApproved, NotAutoApproved
from haku.console.auto_approval.hostexec import BASH_TOOL, evaluate_host_scoped

HOSTS = {"public-coder-devbox"}
RUN_AS = {"coder"}


def approve(**arguments):
    return evaluate_host_scoped(BASH_TOOL, arguments, HOSTS, RUN_AS)


def test_the_configured_host_and_run_as_auto_approve():
    decision = approve(host="public-coder-devbox", run_as="coder", cmd="echo hi")
    assert isinstance(decision, AutoApproved)
    assert "public-coder-devbox" in decision.explanation


def test_root_run_as_stays_manual():
    """The whole point: hostexecd runs as root to drop into run_as, so an auto-approved root call
    would let the Agent read that host's own root-owned daemon token straight off disk."""
    decision = approve(host="public-coder-devbox", run_as="root", cmd="cat /etc/hostexecd-daemon-token.txt")
    assert isinstance(decision, NotAutoApproved)
    assert "root" in decision.reason


@pytest.mark.parametrize(
    "cmd",
    [pytest.param("echo hi", id="trivial"), pytest.param("curl http://169.254.169.254/", id="anything-else-entirely")],
)
def test_any_cmd_as_the_configured_run_as_auto_approves(cmd):
    """This policy never constrains cmd -- that confinement lives in the egress fence, not here."""
    assert isinstance(approve(host="public-coder-devbox", run_as="coder", cmd=cmd), AutoApproved)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"host": "wyrm2", "run_as": "coder", "cmd": "echo hi"}, id="another-host"),
        pytest.param({"host": "public-coder-devbox", "run_as": "root", "cmd": "echo hi"}, id="root-on-the-host"),
        pytest.param({"host": "public-coder-devbox", "run_as": "nobody", "cmd": "echo hi"}, id="unlisted-run-as"),
        pytest.param({"run_as": "coder", "cmd": "echo hi"}, id="missing-host"),
        pytest.param({"host": "public-coder-devbox", "cmd": "echo hi"}, id="missing-run-as"),
        pytest.param({"host": "", "run_as": "coder", "cmd": "echo hi"}, id="blank-host"),
        pytest.param({"host": "public-coder-devbox", "run_as": "", "cmd": "echo hi"}, id="blank-run-as"),
        pytest.param({"host": ["public-coder-devbox"], "run_as": "coder", "cmd": "echo hi"}, id="list-host"),
    ],
)
def test_calls_reaching_past_the_configured_host_or_run_as_stay_manual(arguments):
    assert isinstance(evaluate_host_scoped(BASH_TOOL, arguments, HOSTS, RUN_AS), NotAutoApproved)


def test_only_the_reviewed_tool_is_handled():
    assert isinstance(
        evaluate_host_scoped("some_other_tool", {"host": "public-coder-devbox", "run_as": "coder"}, HOSTS, RUN_AS),
        NotAutoApproved,
    )


if __name__ == "__main__":
    pytest_bazel.main()
