"""Exercise the pinned upstream LiteLLM image's normal span export path."""

import subprocess
from pathlib import Path

import pytest_bazel

from cluster.cdk8s.litellm import upstream_test_image
from util.oci import load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir


def test_verified_baggage_reaches_exported_litellm_span() -> None:
    image = load_oci_image(upstream_test_image.IMAGE)
    script = Path(__file__).with_name("attribution_export_smoke.py")
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "/app/.venv/bin/python", "-i", image, "-"],
        input=script.read_bytes(),
        capture_output=True,
        check=False,
    )
    output = undeclared_outputs_dir() / "litellm_attribution_export.log"
    output.write_bytes(result.stdout + result.stderr)
    assert result.returncode == 0, f"LiteLLM attribution export smoke failed; see {output}"


if __name__ == "__main__":
    pytest_bazel.main()
