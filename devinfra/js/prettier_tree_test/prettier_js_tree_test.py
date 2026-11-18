import os
from pathlib import Path

from python.runfiles import Runfiles


def _tree(runfiles: Runfiles, name: str) -> Path:
    location = runfiles.Rlocation(os.environ[name])
    assert location is not None
    return Path(location)


def test_default_config_formats_the_complete_tree_and_preserves_other_bytes() -> None:
    runfiles = Runfiles.CreateOrRaise()
    output = _tree(runfiles, "ALL_TREE")

    assert (output / "outside.js").read_text() == "const outside = { answer: 42 };\n"
    assert (output / "app" / "main.js").read_text() == 'const message = "hello";\nconsole.log(message);\n'
    assert (output / "app" / "nested" / "payload.bin").read_bytes() == bytes([0, 255, 128, 10, 13, 1])
    assert (output / "app" / "nested" / "note.txt").read_bytes() == bytes([0x68, 0x69, 0x00, 0xFE])


def test_config_and_input_subdirectory_overrides_apply_to_the_selected_tree() -> None:
    runfiles = Runfiles.CreateOrRaise()
    output = _tree(runfiles, "APP_TREE")

    assert (output / "main.js").read_text() == "const message = 'hello'\nconsole.log(message)\n"
    assert (output / "nested" / "values.js").read_text() == "export const values = [1, 2, 3]\n"
    assert (output / "nested" / "payload.bin").read_bytes() == bytes([0, 255, 128, 10, 13, 1])
    assert not (output / "outside.js").exists()
