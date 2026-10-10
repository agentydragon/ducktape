"""Turn a `tofu providers mirror` directory into an image layer tar holding the same providers in
the unpacked layout under `prefix`.

`providers mirror` writes the packed layout (`HOST/NAMESPACE/TYPE/terraform-provider-TYPE_VERSION_OS_ARCH.zip`
plus JSON indexes). From a packed mirror `tofu init` unzips every provider into each run's
`.terraform/`; from an unpacked one (`HOST/NAMESPACE/TYPE/VERSION/OS_ARCH/`) it symlinks the
directory instead, so a runner pod writes nothing per provider.

The tar is reproducible: sorted entries, zero mtimes, root ownership. Its digest changes only when a
provider does, and the image push dedupes on that digest.
"""

from __future__ import annotations

import argparse
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

_PACKAGE = re.compile(r"terraform-provider-(?P<type>[^_]+)_(?P<version>[^_]+)_(?P<target>[^_]+_[^_]+)\.zip")


def _entry(name: PurePosixPath, kind: bytes, mode: int, size: int = 0) -> tarfile.TarInfo:
    info = tarfile.TarInfo(str(name))
    info.type = kind
    info.mode = mode
    info.size = size
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def write_layer(mirror: Path, prefix: PurePosixPath, output: Path) -> None:
    packages = sorted(mirror.rglob("terraform-provider-*.zip"))
    if not packages:
        raise ValueError(f"no provider packages under {mirror=}")
    files: dict[PurePosixPath, tuple[Path, zipfile.ZipInfo]] = {}
    for package in packages:
        match = _PACKAGE.fullmatch(package.name)
        if match is None:
            raise ValueError(f"not a provider package name: {package}")
        source = PurePosixPath(package.parent.relative_to(mirror).as_posix())
        if source.name != match["type"]:
            raise ValueError(f"{package} is not under its provider type's directory")
        with zipfile.ZipFile(package) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    files[prefix / source / match["version"] / match["target"] / member.filename] = (package, member)

    directories = sorted({parent for path in files for parent in path.parents if parent != PurePosixPath(".")})
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as layer:
        for directory in directories:
            layer.addfile(_entry(directory, tarfile.DIRTYPE, 0o755))
        for path, (package, member) in sorted(files.items()):
            executable = (member.external_attr >> 16) & stat.S_IXUSR
            with zipfile.ZipFile(package) as archive, archive.open(member) as content:
                layer.addfile(_entry(path, tarfile.REGTYPE, 0o755 if executable else 0o644, member.file_size), content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mirror", type=Path, required=True, help="A `tofu providers mirror` output directory.")
    parser.add_argument("--prefix", type=PurePosixPath, required=True, help="Layer path of the unpacked mirror.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_layer(args.mirror, PurePosixPath(args.prefix.as_posix().lstrip("/")), args.output)


if __name__ == "__main__":
    main()
