"""Update a container image pin in devinfra/image_pins.json.

Usage:
    python3 devinfra/update_image_pin.py <name> --digest sha256:abc... [--image ghcr.io/...]
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

_DEVINFRA = Path(__file__).parent
_PINS_FILE = _DEVINFRA / "image_pins.json"
_BBRC = _DEVINFRA.parent / ".bbrc"


def _update_runner_config(image: str, digest: str) -> None:
    """Keep bb remote's native runner default in sync with the image pin."""
    lines = _BBRC.read_text().splitlines()
    prefix = "remote --container_image="
    matches = [i for i, line in enumerate(lines) if line.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"Expected one {prefix} entry in {_BBRC}")
    lines[matches[0]] = f"{prefix}docker://{image}@{digest}"
    _BBRC.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Update a container image pin.")
    parser.add_argument("name", help="Image name (key in image_pins.json)")
    parser.add_argument("--digest", required=True, help="New digest (sha256:...)")
    parser.add_argument(
        "--image",
        help="Re-home the pin to this registry repo. A digest reference is repo-scoped, so a pin "
        "seeded against one repo needs both fields moved the first time its own image is published.",
    )
    args = parser.parse_args()

    pins = json.loads(_PINS_FILE.read_text())
    if args.name not in pins:
        parser.error(f"Unknown image: {args.name} (known: {', '.join(sorted(pins))})")

    pins[args.name]["digest"] = args.digest
    if args.image:
        pins[args.name]["image"] = args.image
    _PINS_FILE.write_text(json.dumps(pins, indent=2) + "\n")

    prettier = shutil.which("prettier")
    if prettier:
        subprocess.run([prettier, "--write", _PINS_FILE], check=True)

    print(f"Updated {args.name} in {_PINS_FILE}")

    if args.name == "buildbuddy_remote_runner":
        _update_runner_config(pins[args.name]["image"], args.digest)
        print(f"Updated container_image in {_BBRC}")


if __name__ == "__main__":
    main()
