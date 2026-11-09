"""Process-level checks of the nix-fast-build to Attic stream."""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import pytest_bazel


def _store_path(name: str) -> str:
    return f"/nix/store/{name * 32}-{name}"


def _run(
    tmp_path: Path,
    cache: str,
    records: list[dict],
    *,
    build_exit: int = 0,
    attic_exit: int = 0,
    wait_for_upload: bool = False,
    early_attic_exit: bool = False,
    cancel_after_upload: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str], list[str]]:
    builder = tmp_path / "nix-fast-build"
    attic = tmp_path / "attic"
    fifo = tmp_path / "upload-started"
    os.mkfifo(fifo)
    builder.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """\
            import json
            import os
            import signal
            import sys

            signal.alarm(5)
            with open(os.environ["PID_BUILD"], "w") as f:
                f.write(str(os.getpid()))
            with open(os.environ["ARGS_BUILD"], "w") as f:
                json.dump(sys.argv[1:], f)
            for index, result in enumerate(json.loads(os.environ["RECORDS"])):
                print(json.dumps(result), flush=True)
                if index == 0 and os.environ["WAIT_FOR_UPLOAD"] == "1":
                    with open(os.environ["FIFO"], "rb") as f:
                        assert f.read(1) == b"1"
                if index == 0 and os.environ["HOLD_AFTER_FIRST"] == "1":
                    signal.pause()
            raise SystemExit(int(os.environ["BUILD_EXIT"]))
            """
        )
    )
    attic.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """\
            import json
            import os
            import sys

            with open(os.environ["PID_ATTIC"], "w") as f:
                f.write(str(os.getpid()))
            with open(os.environ["ARGS_ATTIC"], "w") as f:
                json.dump(sys.argv[1:], f)
            if os.environ["EARLY_ATTIC_EXIT"] == "1":
                raise SystemExit(7)
            for index, line in enumerate(sys.stdin):
                with open(os.environ["PUSHED"], "a") as f:
                    f.write(line)
                if index == 0 and os.environ["WAIT_FOR_UPLOAD"] == "1":
                    with open(os.environ["FIFO"], "wb") as f:
                        f.write(b"1")
                if index == 0 and os.environ["HOLD_AFTER_FIRST"] == "1":
                    print("UPLOAD_STARTED", file=sys.stderr, flush=True)
            raise SystemExit(int(os.environ["ATTIC_EXIT"]))
            """
        )
    )
    builder.chmod(0o755)
    attic.chmod(0o755)
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "RECORDS": json.dumps(records),
        "BUILD_EXIT": str(build_exit),
        "ATTIC_EXIT": str(attic_exit),
        "WAIT_FOR_UPLOAD": "1" if wait_for_upload else "0",
        "EARLY_ATTIC_EXIT": "1" if early_attic_exit else "0",
        "HOLD_AFTER_FIRST": "1" if cancel_after_upload else "0",
        "FIFO": str(fifo),
        "PID_BUILD": str(tmp_path / "build.pid"),
        "PID_ATTIC": str(tmp_path / "attic.pid"),
        "ARGS_BUILD": str(tmp_path / "build-args.json"),
        "ARGS_ATTIC": str(tmp_path / "attic-args.json"),
        "PUSHED": str(tmp_path / "pushed.txt"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "devinfra.ci.nix_attic_build_and_push", cache],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        if cancel_after_upload:
            assert process.stderr is not None
            assert process.stderr.readline() == "UPLOAD_STARTED\n"
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail("streaming uploader deadlocked")
    completed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    pushed_file = tmp_path / "pushed.txt"
    attic_args_file = tmp_path / "attic-args.json"
    return (
        completed,
        pushed_file.read_text().splitlines() if pushed_file.exists() else [],
        json.loads((tmp_path / "build-args.json").read_text()),
        json.loads(attic_args_file.read_text()) if attic_args_file.exists() else [],
    )


def test_main_streams_completed_unique_outputs_even_when_build_fails(tmp_path: Path) -> None:
    first, second, third = (_store_path(name) for name in ("a", "b", "c"))
    result, pushed, build_args, attic_args = _run(
        tmp_path,
        "main",
        [
            {"type": "BUILD", "success": True, "outputs": {"out": first}},
            {"type": "BUILD", "success": True, "outputs": {"out": first}},
            {"type": "BUILD", "success": False, "outputs": {"out": second}},
            {"type": "BUILD", "success": True, "outputs": {"out": second, "dev": third}},
        ],
        build_exit=1,
        wait_for_upload=True,
    )
    assert result.returncode == 1
    assert pushed == [first, second, third]
    assert "--stream-json-lines" in build_args
    assert "--skip-cached" in build_args
    assert build_args[-1] == ".#atticPushTargets.x86_64-linux.main"
    assert attic_args == ["push", "--stdin", "--jobs", "1", "ducktape:main"]


def test_public_pushes_local_output_without_skip_cached(tmp_path: Path) -> None:
    existing = _store_path("d")
    result, pushed, build_args, attic_args = _run(
        tmp_path,
        "public",
        [
            {"type": "EVAL", "success": True, "cacheStatus": "local", "outputs": {"out": existing}},
            {"type": "BUILD", "success": True, "outputs": {"out": existing}},
        ],
    )
    assert result.returncode == 0
    assert pushed == [existing]
    assert "--skip-cached" not in build_args
    assert build_args[-1] == ".#atticPushTargets.x86_64-linux.public"
    assert attic_args[-1] == "ducktape:public"


@pytest.mark.parametrize("early", [False, True])
def test_uploader_failure_fails_job_without_deadlock(tmp_path: Path, early: bool) -> None:
    result, _, _, _ = _run(
        tmp_path,
        "main",
        [{"type": "BUILD", "success": True, "outputs": {"out": _store_path("g")}}],
        attic_exit=7,
        early_attic_exit=early,
    )
    assert result.returncode == 1
    assert "attic push" in result.stderr


def test_no_successful_build_does_not_start_attic(tmp_path: Path) -> None:
    result, pushed, _, attic_args = _run(
        tmp_path, "main", [{"type": "BUILD", "success": False, "outputs": {"out": _store_path("h")}}], build_exit=1
    )
    assert result.returncode == 1
    assert pushed == []
    assert attic_args == []


def test_non_out_output_is_pushed_as_in_nix_fast_build_1_6(tmp_path: Path) -> None:
    dev = _store_path("j")
    result, pushed, _, _ = _run(tmp_path, "public", [{"type": "BUILD", "success": True, "outputs": {"dev": dev}}])
    assert result.returncode == 0
    assert pushed == [dev]


def test_success_with_no_outputs_skips_attic(tmp_path: Path) -> None:
    result, pushed, _, attic_args = _run(tmp_path, "public", [{"type": "BUILD", "success": True}])
    assert result.returncode == 0
    assert pushed == []
    assert attic_args == []


def test_cancellation_stops_builder_and_uploader(tmp_path: Path) -> None:
    result, pushed, _, _ = _run(
        tmp_path,
        "main",
        [{"type": "BUILD", "success": True, "outputs": {"out": _store_path("k")}}],
        cancel_after_upload=True,
    )
    assert result.returncode == 143
    assert pushed == [_store_path("k")]
    for name in ("build.pid", "attic.pid"):
        pid = int((tmp_path / name).read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    pytest_bazel.main()
