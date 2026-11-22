import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest
import pytest_bazel

from cluster.tf_runner.plugin_layer import write_layer


def test_equivalent_provider_packages_produce_the_same_unpacked_layer(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    source = Path("registry.example.test/example-ns/widget")
    directory = mirror / source
    directory.mkdir(parents=True)
    (directory / "index.json").write_text("{}")
    package = directory / "terraform-provider-widget_1.2.3-rc.1_linux_amd64.zip"
    binary = "terraform-provider-widget_v1.2.3-rc.1"
    entries = [(binary, 0o755), ("LICENSE", 0o644)]

    def build_layer(year: int, members: list[tuple[str, int]]) -> Path:
        with zipfile.ZipFile(package, "w") as archive:
            for name, mode in members:
                info = zipfile.ZipInfo(name, date_time=(year, 1, 1, 0, 0, 0))
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, name.encode())
        output = tmp_path / f"{year}.tar"
        write_layer(mirror, PurePosixPath("opt/plugins"), output)
        return output

    first = build_layer(2020, entries)
    repacked = build_layer(2021, list(reversed(entries)))
    assert first.read_bytes() == repacked.read_bytes()

    target = f"opt/plugins/{source}/1.2.3-rc.1/linux_amd64"
    with tarfile.open(first) as layer:
        assert layer.getmember("opt").isdir()
        files = {member.name: member for member in layer if member.isfile()}
        assert set(files) == {f"{target}/{name}" for name, _ in entries}
        for name, mode in entries:
            member = files[f"{target}/{name}"]
            assert member.mode == mode
            content = layer.extractfile(member)
            assert content is not None
            assert content.read() == name.encode()


def test_rejects_an_empty_mirror(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no provider packages"):
        write_layer(tmp_path, PurePosixPath("opt/plugins"), tmp_path / "layer.tar")


if __name__ == "__main__":
    pytest_bazel.main()
