"""Exercise the SysRq worker's proof gate without writing to the host kernel."""

import os
import subprocess
import tarfile
from pathlib import Path

import pytest_bazel
from python.runfiles import Runfiles


def test_worker_skips_after_proof_and_requests_dump_without_proof(tmp_path: Path) -> None:
    runfiles = Runfiles.CreateOrRaise()
    script = runfiles.Rlocation(os.environ["SYSRQ_SCRIPT_RLOCATION"])
    busybox_tar = runfiles.Rlocation(os.environ["SYSRQ_BUSYBOX_RLOCATION"])
    assert script is not None
    assert busybox_tar is not None
    busybox = tmp_path / "busybox"
    with tarfile.open(busybox_tar) as archive:
        member = next(member for member in archive if member.name.endswith("bin/busybox"))
        source = archive.extractfile(member)
        assert source is not None
        busybox.write_bytes(source.read())
    busybox.chmod(0o755)
    proof = tmp_path / "proof"
    trigger = tmp_path / "trigger"
    trigger.write_text("unchanged")
    env = os.environ | {"AGENTPLANE_SYSRQ_BUSYBOX": str(busybox)}

    proof.touch()
    skipped = subprocess.run(
        [str(busybox), "sh", script, str(proof), str(trigger), "0"], env=env, text=True, capture_output=True, check=True
    )
    assert "AGENTPLANE_SYSRQ_SKIPPED reason=proof" in skipped.stdout
    assert "AGENTPLANE_SYSRQ_BEGIN" not in skipped.stdout
    assert trigger.read_text() == "unchanged"

    proof.unlink()
    attempted = subprocess.run(
        [str(busybox), "sh", script, str(proof), str(trigger), "0"], env=env, text=True, capture_output=True, check=True
    )
    assert "AGENTPLANE_SYSRQ_BEGIN command=w" in attempted.stdout
    assert "AGENTPLANE_SYSRQ_END command=w status=0" in attempted.stdout
    assert trigger.read_text() == "w\n"


if __name__ == "__main__":
    pytest_bazel.main()
