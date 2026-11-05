import os
import subprocess

from python.runfiles import Runfiles


def test_root_run_is_the_fixture_symlink_in_all_images() -> None:
    runfiles_root = Runfiles.CreateOrRaise().root()
    closure = runfiles_root / os.environ["EROFS_CLOSURE_RLOCATION"]
    tool = runfiles_root / os.environ["EROFS_TOOL_RLOCATION"]

    for image_key in ("ROOT_IMAGE_RLOCATION", "ROOT_PROFILE_IMAGE_RLOCATION"):
        image = runfiles_root / os.environ[image_key]

        inode = subprocess.run(
            [str(tool), str(closure), "dump.erofs", "--path=/var/run", str(image)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "symlink file" in inode, f"{image_key}: {inode}"

        target = subprocess.run(
            [str(tool), str(closure), "dump.erofs", "--cat", "--path=/var/run", str(image)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert target == "/run", f"{image_key}: {target}"


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
