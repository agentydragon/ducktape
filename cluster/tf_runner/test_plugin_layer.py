import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest
import pytest_bazel

from cluster.tf_runner.plugin_layer import write_layer


def _package(mirror: Path, source: str, name: str, members: dict[str, int]) -> None:
    (mirror / source).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(mirror / source / name, "w") as archive:
        for member, mode in members.items():
            info = zipfile.ZipInfo(member)
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, f"contents of {member}")


@pytest.fixture
def mirror(tmp_path: Path) -> Path:
    mirror = tmp_path / "mirror"
    _package(
        mirror,
        "registry.example.test/example-ns/widget",
        "terraform-provider-widget_1.2.3-rc.1_linux_amd64.zip",
        {"terraform-provider-widget_v1.2.3-rc.1": 0o755, "LICENSE": 0o644},
    )
    (mirror / "registry.example.test/example-ns/widget/index.json").write_text("{}")
    return mirror


def test_unpacks_into_version_and_target_directories(mirror: Path, tmp_path: Path) -> None:
    write_layer(mirror, PurePosixPath("opt/plugins"), tmp_path / "layer.tar")

    with tarfile.open(tmp_path / "layer.tar") as layer:
        files = {member.name: member for member in layer.getmembers() if member.isfile()}
        target = "opt/plugins/registry.example.test/example-ns/widget/1.2.3-rc.1/linux_amd64"
        assert set(files) == {f"{target}/terraform-provider-widget_v1.2.3-rc.1", f"{target}/LICENSE"}
        assert files[f"{target}/terraform-provider-widget_v1.2.3-rc.1"].mode == 0o755
        assert files[f"{target}/LICENSE"].mode == 0o644
        assert layer.getmember("opt").isdir()


def test_is_reproducible(mirror: Path, tmp_path: Path) -> None:
    write_layer(mirror, PurePosixPath("opt/plugins"), tmp_path / "first.tar")
    write_layer(mirror, PurePosixPath("opt/plugins"), tmp_path / "second.tar")

    assert (tmp_path / "first.tar").read_bytes() == (tmp_path / "second.tar").read_bytes()


def test_rejects_an_empty_mirror(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no provider packages"):
        write_layer(tmp_path, PurePosixPath("opt/plugins"), tmp_path / "layer.tar")


if __name__ == "__main__":
    pytest_bazel.main()
