"""Pack pinned OCI layouts into one archive of digest-addressed image blobs."""

import io
import json
import sys
import tarfile
from itertools import batched
from pathlib import Path


def main() -> None:
    output, *args = sys.argv[1:]
    manifests = []
    blobs: dict[str, Path] = {}
    for reference, directory in batched(args, 2, strict=True):
        layout = Path(directory)
        index = json.loads((layout / "index.json").read_text())
        (descriptor,) = index["manifests"]
        descriptor.setdefault("annotations", {})["org.opencontainers.image.ref.name"] = reference
        manifests.append(descriptor)
        for blob in (layout / "blobs").glob("*/*"):
            blobs[blob.relative_to(layout).as_posix()] = blob

    with tarfile.open(output, "w") as archive:
        for name, value in (
            ("oci-layout", {"imageLayoutVersion": "1.0.0"}),
            ("index.json", {"schemaVersion": 2, "manifests": manifests}),
        ):
            encoded = json.dumps(value, sort_keys=True).encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(encoded)
            archive.addfile(entry, io.BytesIO(encoded))
        for name, path in sorted(blobs.items()):
            entry = tarfile.TarInfo(name)
            entry.size = path.stat().st_size
            with path.open("rb") as source:
                archive.addfile(entry, source)


if __name__ == "__main__":
    main()
