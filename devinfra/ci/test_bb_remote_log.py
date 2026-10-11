"""The Actions entry shell must forward cancellation while bb is still running."""

import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_bazel

SCRIPT = Path(__file__).with_name("bb_remote_log.sh")
# Match the composite's errexit handling and status capture after sourcing.
SHELL = 'set +e; source "$1" "$2" "${@:3}"; status=$?; exit "$status"'


@pytest.mark.parametrize("exit_code", [0, 7])
def test_log_is_drained_and_command_status_preserved(tmp_path: Path, exit_code: int) -> None:
    log = tmp_path / "remote.log"
    result = subprocess.run(
        [
            "bash",
            "-c",
            SHELL,
            "step",
            str(SCRIPT),
            str(log),
            sys.executable,
            "-c",
            f'import sys; print("out"); print("err", file=sys.stderr); raise SystemExit({exit_code})',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code
    assert sorted(log.read_text().splitlines()) == ["err", "out"]
    assert result.stdout == log.read_text()


@pytest.mark.parametrize("cancel_signal", [signal.SIGINT, signal.SIGTERM])
def test_entry_shell_cancels_running_child(tmp_path: Path, cancel_signal: signal.Signals) -> None:
    log = tmp_path / "remote.log"
    child = """
import signal

def cancel(signum, frame):
    print("remote cancellation requested", flush=True)
    raise SystemExit(1)

signal.signal(signal.SIGTERM, cancel)
print("ready", flush=True)
signal.pause()
"""
    process = subprocess.Popen(
        ["bash", "-c", SHELL, "step", str(SCRIPT), str(log), sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        assert select.select([process.stdout], [], [], 5)[0], "child did not start"
        assert process.stdout.readline() == "ready\n"
        # GitHub signals the entry process, not the process group, first.
        process.send_signal(cancel_signal)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 130
        assert "remote cancellation requested" in stdout
        assert "forwarding cancellation" in stderr
        assert log.read_text() == "ready\nremote cancellation requested\n"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


if __name__ == "__main__":
    pytest_bazel.main()
