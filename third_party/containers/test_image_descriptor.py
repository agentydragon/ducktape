"""The generated image descriptor carries a usable OCI layout in its runfiles."""

from python.runfiles import Runfiles

from third_party.containers import debian_slim


def test_generated_image_descriptor_names_packaged_layout() -> None:
    layout = Runfiles.CreateOrRaise().root() / debian_slim.LAYOUT_RLOCATION
    assert (layout / "index.json").is_file()
