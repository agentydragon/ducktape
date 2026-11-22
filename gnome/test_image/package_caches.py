"""Build the GNOME package caches omitted by the Debian package layer."""

import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def _layer_metadata(entry: tarfile.TarInfo) -> tarfile.TarInfo:
    entry.uid = entry.gid = entry.mtime = 0
    entry.uname = entry.gname = "root"
    entry.mode = 0o755 if entry.isdir() else 0o644
    return entry


def main() -> None:
    closure, output = map(Path, sys.argv[1:])
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(["tar", "-xf", closure, "-C", root], check=True)
        libdir = root / "usr/lib/x86_64-linux-gnu"
        loader = [libdir / "ld-linux-x86-64.so.2", "--library-path", libdir]
        share = root / "usr/share"
        schemas = share / "glib-2.0/schemas"
        mime = share / "mime"
        subprocess.run([*loader, root / "usr/bin/glib-compile-schemas", schemas], check=True)
        subprocess.run(
            [*loader, root / "usr/bin/update-mime-database", mime],
            env={**os.environ, "XDG_DATA_DIRS": str(share)},
            check=True,
        )

        pixbuf = libdir / "gdk-pixbuf-2.0"
        (modules,) = pixbuf.glob("*/loaders")
        loader_cache = subprocess.check_output(
            [*loader, pixbuf / "gdk-pixbuf-query-loaders"], env={**os.environ, "GDK_PIXBUF_MODULEDIR": str(modules)}
        )
        # The compiler records absolute module paths, which must resolve inside the image.
        (modules.parent / "loaders.cache").write_bytes(loader_cache.replace(os.fsencode(root), b""))

        with tarfile.open(output, "w") as archive:
            generated = [schemas / "gschemas.compiled", modules.parent / "loaders.cache"]
            generated.extend(path for path in mime.iterdir() if path.name != "packages")
            for path in sorted(generated):
                archive.add(path, arcname=path.relative_to(root).as_posix(), filter=_layer_metadata)


if __name__ == "__main__":
    main()
