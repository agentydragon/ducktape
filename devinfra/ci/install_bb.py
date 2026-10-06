"""Install the Nix-pinned BuildBuddy CLI using the GitHub runner's stdlib Python."""

import argparse
import base64
import hashlib
import json
import tempfile
from pathlib import Path
from urllib.request import urlopen


def install_bb(pins: Path, destination: Path) -> None:
    pin = json.loads(pins.read_text())["pins"]["bb"]
    expected_digest = base64.b64decode(pin["sha256"], validate=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp_dir:
        downloaded = Path(temp_dir) / "bb"
        digest = hashlib.sha256()
        with urlopen(pin["url"], timeout=60) as response, downloaded.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.digest() != expected_digest:
            raise ValueError("BuildBuddy CLI download does not match the pinned SHA-256")
        downloaded.chmod(0o755)
        downloaded.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    install_bb(args.pins, args.destination)


if __name__ == "__main__":
    main()
