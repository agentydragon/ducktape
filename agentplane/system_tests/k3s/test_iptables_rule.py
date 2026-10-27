"""Exercise xtables waiting with the iptables binary bundled by k3s on RBE."""

import fcntl
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.system_tests.k3s.iptables_rule import IptablesCommandError, ensure_rule
from util.bazel.runfiles import get_required_path


def _fake_iptables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_result: int) -> tuple[Path, Path]:
    binary = tmp_path / "iptables"
    log = tmp_path / "commands.log"
    binary.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$IPTABLES_COMMAND_LOG"\n'
        'for argument do\n'
        '  if [ "$argument" = "-C" ]; then exit "$IPTABLES_CHECK_RESULT"; fi\n'
        '  if [ "$argument" = "-A" ]; then exit 0; fi\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("IPTABLES_COMMAND_LOG", str(log))
    monkeypatch.setenv("IPTABLES_CHECK_RESULT", str(check_result))
    return binary, log


@pytest.mark.parametrize(("check_result", "expected_calls"), [(0, 1), (1, 2), (4, 1)])
def test_only_a_missing_rule_is_appended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, check_result: int, expected_calls: int
) -> None:
    binary, log = _fake_iptables(tmp_path, monkeypatch, check_result)

    if check_result == 4:
        with pytest.raises(IptablesCommandError) as error:
            ensure_rule(binary, table="filter", chain="DUCKTAPE_TEST", rule=("-j", "RETURN"))
        assert error.value.returncode == 4
    else:
        ensure_rule(binary, table="filter", chain="DUCKTAPE_TEST", rule=("-j", "RETURN"))

    commands = log.read_text(encoding="utf-8").splitlines()
    assert len(commands) == expected_calls
    assert commands[0].startswith("-w 5 -t filter -C DUCKTAPE_TEST ")
    if check_result == 1:
        assert commands[1].startswith("-w 5 -t filter -A DUCKTAPE_TEST ")


@pytest.fixture(scope="module")
def iptables_binary(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    work = tmp_path_factory.mktemp("k3s-iptables")
    data_dir = work / "data"
    log_path = work / "k3s.log"
    binary = get_required_path(os.environ["K3S_BINARY_RLOCATION"])
    iptables = data_dir / "data/current/bin/aux/iptables"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [str(binary), "server", "--data-dir", str(data_dir), "--disable-agent", "--flannel-backend=none"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while not iptables.is_file():
                if process.poll() is not None:
                    raise AssertionError(f"k3s exited before extracting iptables:\n{log_path.read_text()}")
                if time.monotonic() >= deadline:
                    raise AssertionError(f"k3s did not extract iptables:\n{log_path.read_text()}")
                time.sleep(0.02)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    original_path = os.environ["PATH"]
    os.environ["PATH"] = f"{iptables.parent}{os.pathsep}{original_path}"
    try:
        yield iptables
    finally:
        os.environ["PATH"] = original_path


def _run_iptables(iptables: Path, *args: str) -> str:
    result = subprocess.run(
        [str(iptables), "-w", "5", *args], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise AssertionError(
            f"{result.args[:4]} exited {result.returncode}\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        )
    return result.stdout


def _chain_rules(iptables: Path, chain: str) -> list[str]:
    output = _run_iptables(iptables, "-t", "filter", "-S", chain)
    return [line for line in output.splitlines() if line.startswith("-A ")]


def _wait_for_helper_lock_fd(iptables: Path, chain: str) -> int:
    expected_args = [str(iptables), "-w", "5", "-t", "filter", "-C", chain, "-j", "RETURN"]
    lock = Path("/run/xtables.lock").stat()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                args = entry.joinpath("cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
                if args != expected_args:
                    continue
                for fd in entry.joinpath("fd").iterdir():
                    try:
                        opened = fd.stat()
                    except FileNotFoundError:
                        continue
                    if (opened.st_dev, opened.st_ino) == (lock.st_dev, lock.st_ino):
                        return int(entry.name)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
        time.sleep(0.02)
    raise AssertionError("helper's iptables check did not open the held xtables lock")


def _hold_xtables_lock() -> int:
    fd = os.open("/run/xtables.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_xtables_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def test_real_xtables_lock_release_adds_one_missing_rule(iptables_binary: Path) -> None:
    chain = "DT_" + uuid.uuid4().hex[:12].upper()
    iptables = iptables_binary
    _run_iptables(iptables, "-t", "filter", "-N", chain)
    try:
        errors: list[BaseException] = []
        started = threading.Event()
        completed = threading.Event()

        def add_rule() -> None:
            started.set()
            try:
                ensure_rule(iptables, table="filter", chain=chain, rule=("-j", "RETURN"))
            except BaseException as error:
                errors.append(error)
            finally:
                completed.set()

        worker = threading.Thread(target=add_rule, daemon=True)
        lock_fd = _hold_xtables_lock()
        worker_started = False
        try:
            no_wait = subprocess.run(
                [str(iptables), "-t", "filter", "-C", chain, "-j", "RETURN"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert no_wait.returncode == 4
            worker.start()
            worker_started = True
            assert started.wait(timeout=3)
            _wait_for_helper_lock_fd(iptables, chain)
            assert not completed.is_set()
        finally:
            _release_xtables_lock(lock_fd)
            if worker_started:
                worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == []

        ensure_rule(iptables, table="filter", chain=chain, rule=("-j", "RETURN"))
        assert _chain_rules(iptables, chain) == [f"-A {chain} -j RETURN"]
    finally:
        _run_iptables(iptables, "-t", "filter", "-F", chain)
        _run_iptables(iptables, "-t", "filter", "-X", chain)


def test_persistent_xtables_lock_fails_closed(iptables_binary: Path) -> None:
    chain = "DT_" + uuid.uuid4().hex[:12].upper()
    iptables = iptables_binary
    _run_iptables(iptables, "-t", "filter", "-N", chain)
    try:
        lock_fd = _hold_xtables_lock()
        try:
            with pytest.raises(IptablesCommandError) as error:
                ensure_rule(iptables, table="filter", chain=chain, rule=("-j", "RETURN"))
            assert error.value.returncode == 4
        finally:
            _release_xtables_lock(lock_fd)
        assert _chain_rules(iptables, chain) == []
    finally:
        _run_iptables(iptables, "-t", "filter", "-F", chain)
        _run_iptables(iptables, "-t", "filter", "-X", chain)


if __name__ == "__main__":
    pytest_bazel.main()
