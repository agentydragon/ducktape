"""Install the pinned BuildBuddy CLIs on the GitHub runner."""

import argparse
import base64
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from urllib.request import urlopen


def install_clis(pins_path: Path, bin_dir: Path) -> None:
    pins = json.loads(pins_path.read_text())["pins"]
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=bin_dir) as temp_dir:
        staging = Path(temp_dir)
        names = ("bb", "bbapi")
        for name in names:
            pin = pins[name]
            expected_digest = base64.b64decode(pin["sha256"], validate=True)
            downloaded = staging / name
            with urlopen(pin["url"], timeout=60) as response, downloaded.open("wb") as output:
                shutil.copyfileobj(response, output)
            with downloaded.open("rb") as source:
                if hashlib.file_digest(source, "sha256").digest() != expected_digest:
                    raise ValueError(f"{name} download does not match the pinned SHA-256")
            downloaded.chmod(0o755)

        for name in names:
            (staging / name).replace(bin_dir / name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--bin-dir", type=Path, required=True)
    args = parser.parse_args()
    install_clis(args.pins, args.bin_dir)


if __name__ == "__main__":
    main()
