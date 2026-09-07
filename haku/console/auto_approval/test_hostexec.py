"""The hostexec host-scoping boundary: one VM, any run_as, any cmd."""

from __future__ import annotations

import pytest
import pytest_bazel

from haku.console.auto_approval.decision import AutoApproved, NotAutoApproved
from haku.console.auto_approval.hostexec import BASH_TOOL, evaluate_host_scoped

HOSTS = {"public-coder-devbox"}


def approve(**arguments):
    return evaluate_host_scoped(BASH_TOOL, arguments, HOSTS)


def test_the_configured_host_auto_approves():
    decision = approve(host="public-coder-devbox", run_as="coder", cmd="echo hi")
    assert isinstance(decision, AutoApproved)
    assert "public-coder-devbox" in decision.explanation


def test_root_run_as_also_auto_approves():
    """The whole point: this policy never constrains run_as, unlike a hypothetical narrower one."""
    decision = approve(host="public-coder-devbox", run_as="root", cmd="rm -rf /some/scoped/path")
    assert isinstance(decision, AutoApproved)


@pytest.mark.parametrize(
    "cmd",
    [pytest.param("echo hi", id="trivial"), pytest.param("curl http://169.254.169.254/", id="anything-else-entirely")],
)
def test_any_cmd_on_the_configured_host_auto_approves(cmd):
    """This policy never constrains cmd -- that confinement lives in the egress fence, not here."""
    assert isinstance(approve(host="public-coder-devbox", run_as="coder", cmd=cmd), AutoApproved)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"host": "wyrm2", "run_as": "agentydragon", "cmd": "echo hi"}, id="another-host"),
        pytest.param({"host": "rugged", "run_as": "root", "cmd": "echo hi"}, id="another-host-root"),
        pytest.param({"run_as": "coder", "cmd": "echo hi"}, id="missing-host"),
        pytest.param({"host": "", "run_as": "coder", "cmd": "echo hi"}, id="blank-host"),
        pytest.param({"host": ["public-coder-devbox"], "run_as": "coder", "cmd": "echo hi"}, id="list-host"),
    ],
)
def test_calls_reaching_past_the_configured_host_stay_manual(arguments):
    assert isinstance(evaluate_host_scoped(BASH_TOOL, arguments, HOSTS), NotAutoApproved)


def test_only_the_reviewed_tool_is_handled():
    assert isinstance(evaluate_host_scoped("some_other_tool", {"host": "public-coder-devbox"}, HOSTS), NotAutoApproved)


if __name__ == "__main__":
    pytest_bazel.main()
