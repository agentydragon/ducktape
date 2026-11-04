"""The generated image descriptor carries a usable OCI layout in its runfiles."""

import pytest_bazel

from devinfra.bazel.runfiles import get_required_path
from third_party.containers import debian_slim


def test_generated_image_descriptor_names_packaged_layout() -> None:
    layout = get_required_path(debian_slim.LAYOUT_RLOCATION)
    assert (layout / "index.json").is_file()


if __name__ == "__main__":
    pytest_bazel.main()
