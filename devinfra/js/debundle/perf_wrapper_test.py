"""Exercise the whole perf wrapper with a deterministic external perf command."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("status", [0, 7, 124])
def test_postprocessing_preserves_status_and_partial_output(tmp_path: Path, status: int) -> None:
    perf = tmp_path / "perf"
    perf.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == record ]]; then exit 0; fi\n'
        "echo partial-report\n"
        "echo diagnostic >&2\n"
        f"exit {status}\n"
    )
    perf.chmod(0o755)
    output = tmp_path / "profile"
    subprocess.run(
        ["bash", str(Path(__file__).with_name("perf_wrapper.sh")), "--output-dir", str(output), "--", "true"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=True,
        capture_output=True,
        text=True,
    )
    report = output / "perf_report_flat_symbols.txt"
    failure = report.with_suffix(".txt.failed.txt")
    if status:
        assert f"status={status}\n" in failure.read_text()
        assert "diagnostic" in failure.read_text()
        assert report.with_suffix(".txt.partial").read_text() == "partial-report\n"
        assert not report.exists()
    else:
        assert report.read_text() == "partial-report\n"
        assert report.with_suffix(".txt.stderr").read_text() == "diagnostic\n"
        assert not failure.exists()
    assert not list(output.glob("*.tmp"))
