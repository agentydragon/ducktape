"""Install a checksum-pinned Home Assistant Dreo component into /config."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "2.2.1"
URL = f"https://github.com/dreo-team/hass-dreoverse/archive/refs/tags/v{VERSION}.zip"
SHA256 = "555e4e3470b64574bfe5a050ca5557ce3f509844e2e8827ecd7e10c8a3d77d24"
DOMAIN = "dreo"


def _safe_extract(payload: bytes, destination: Path) -> None:
    """Extract an archive while rejecting members outside its destination."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = destination.resolve()
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"unsafe archive member: {member.filename}")
        archive.extractall(destination)


def _component_source(extracted: Path) -> Path:
    """Find the single Dreo component in the extracted source archive."""
    candidates = sorted(extracted.glob("*/custom_components/dreo"))
    if len(candidates) != 1:
        raise ValueError(f"expected one Dreo component, found {len(candidates)}")
    return candidates[0]


def install(config_dir: Path, payload: bytes) -> None:
    """Validate and atomically replace the Dreo component in the HA config."""
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256:
        raise ValueError(f"Dreo archive SHA256 mismatch: got {digest}, want {SHA256}")

    components = config_dir / "custom_components"
    target = components / DOMAIN
    with tempfile.TemporaryDirectory(dir=config_dir) as temporary:
        extracted = Path(temporary) / "source"
        extracted.mkdir()
        _safe_extract(payload, extracted)
        source = _component_source(extracted)
        manifest = json.loads((source / "manifest.json").read_text())
        if manifest.get("domain") != DOMAIN:
            raise ValueError(f"Dreo manifest has unexpected domain: {manifest.get('domain')}")
        if manifest.get("version") != VERSION:
            raise ValueError(f"Dreo manifest has unexpected version: {manifest.get('version')}")

        replacement = components / f".{DOMAIN}.new"
        shutil.rmtree(replacement, ignore_errors=True)
        components.mkdir(exist_ok=True)
        shutil.copytree(source, replacement)
        shutil.rmtree(target, ignore_errors=True)
        replacement.rename(target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", type=Path, default=Path("/config"))
    args = parser.parse_args()
    manifest = args.config_dir / "custom_components" / DOMAIN / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()).get("version") == VERSION:
        return
    with urllib.request.urlopen(URL, timeout=60) as response:
        install(args.config_dir, response.read())


if __name__ == "__main__":
    main()
