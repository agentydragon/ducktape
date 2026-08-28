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
_BBR_CONFIG = _DEVINFRA / "bbr.json"


def _update_bbr_config(image: str, digest: str) -> bool:
    """Update container_image in bbr.json when bbr_runner is repinned.

    bbr.json names the `bb remote` runner, deliberately not the RBE worker: the
    runner's digest is in no action's cache key, so it can move freely.
    """
    if not _BBR_CONFIG.exists():
        return False
    config = json.loads(_BBR_CONFIG.read_text())
    if "container_image" not in config:
        return False
    config["container_image"] = f"{image}@{digest}"
    _BBR_CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    return True


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

    # Also update bbr.json when the runner is repinned
    if args.name == "bbr_runner":
        image = pins[args.name]["image"]
        if _update_bbr_config(image, args.digest):
            if prettier:
                subprocess.run([prettier, "--write", _BBR_CONFIG], check=True)
            print(f"Updated container_image in {_BBR_CONFIG}")


if __name__ == "__main__":
    main()
