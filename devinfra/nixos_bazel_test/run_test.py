import io
import os
import subprocess
import tarfile
import uuid
from contextlib import ExitStack
from pathlib import Path

import pytest_bazel
from python.runfiles import runfiles


def test_runner_cleans_scratch_after_docker_start_failure(tmp_path: Path) -> None:
    image = f"nixos-bazel-run-test-{uuid.uuid4().hex}:latest"
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w"):
        pass
    with ExitStack() as stack:
        subprocess.run(["docker", "import", "-", image], input=archive.getvalue(), check=True, capture_output=True)
        stack.callback(subprocess.run, ["docker", "image", "rm", "-f", image], check=False, capture_output=True)

        files = runfiles.Create()
        assert files is not None
        script = files.Rlocation("ducktape/devinfra/nixos_bazel_test/run.sh")
        assert script is not None
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        bazelrc = tmp_path / "rbe.bazelrc"
        bazelrc.touch()
        environment = os.environ | {"TMPDIR": str(scratch), "BUILDBUDDY_API_KEY": "test-key"}
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; IMAGE_NAME="$2"; TEST_WORKSPACE="$3"; RBE_BAZELRC="$4"; run_container test',
                "bash",
                script,
                image.removesuffix(":latest"),
                str(workspace),
                str(bazelrc),
            ],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "/init" in result.stderr
        assert not list(scratch.iterdir())
        containers = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"ancestor={image}"], check=True, capture_output=True, text=True
        )
        assert not containers.stdout.strip()


if __name__ == "__main__":
    pytest_bazel.main()
