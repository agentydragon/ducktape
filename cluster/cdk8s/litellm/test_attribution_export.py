"""Exercise the pinned upstream LiteLLM image's normal span export path."""

import logging
import subprocess
from pathlib import Path

import pytest_bazel

from cluster.cdk8s.litellm import upstream_test_image
from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


def test_verified_baggage_reaches_exported_litellm_span() -> None:
    output = undeclared_outputs_dir() / "litellm_attribution_export.log"
    with output.open("w") as log:
        handler = logging.StreamHandler(log)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
        image_logger = logging.getLogger("devinfra.oci.oci")
        previous_level = image_logger.level
        image_logger.addHandler(handler)
        image_logger.setLevel(logging.INFO)
        try:
            image = load_oci_image(upstream_test_image.LAYOUT_RLOCATION)
            log.write("Image loaded; starting smoke process\n")
            log.flush()
            script = Path(__file__).with_name("attribution_export_smoke.py")
            result = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "/app/.venv/bin/python", "-i", image, "-"],
                input=script.read_bytes(),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        finally:
            image_logger.removeHandler(handler)
            image_logger.setLevel(previous_level)
    assert result.returncode == 0, f"LiteLLM attribution export smoke failed; see {output}"


if __name__ == "__main__":
    pytest_bazel.main()
