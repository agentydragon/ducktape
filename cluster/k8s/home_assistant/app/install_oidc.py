"""Install a checksum-pinned Home Assistant OIDC component into /config."""

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

VERSION = "1.1.1"
URL = f"https://github.com/christiaangoossens/hass-oidc-auth/releases/download/v{VERSION}/hass-oidc-auth.zip"
SHA256 = "9ce9e6153f80c781e360b93e097ff7d87d09235430fc48e7a67d97dda5fc3322"
CONFIG_FILES = ("automations.yaml", "scripts.yaml", "scenes.yaml")


def initialize_config(config_dir: Path) -> None:
    """Create HA's mutable YAML files without overwriting user configuration."""
    for name in CONFIG_FILES:
        path = config_dir / name
        if not path.exists():
            path.write_text("[]\n")


def _safe_extract(payload: bytes, destination: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = destination.resolve()
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"unsafe archive member: {member.filename}")
        archive.extractall(destination)


def install(config_dir: Path, payload: bytes) -> None:
    digest = hashlib.sha256(payload).hexdigest()
    if digest != SHA256:
        raise ValueError(f"OIDC archive SHA256 mismatch: got {digest}, want {SHA256}")

    components = config_dir / "custom_components"
    target = components / "auth_oidc"
    with tempfile.TemporaryDirectory(dir=config_dir) as temporary:
        staged = Path(temporary) / "auth_oidc"
        staged.mkdir()
        _safe_extract(payload, staged)
        manifest = json.loads((staged / "manifest.json").read_text())
        if manifest.get("version") != VERSION:
            raise ValueError(f"OIDC manifest has unexpected version: {manifest.get('version')}")
        replacement = components / ".auth_oidc.new"
        shutil.rmtree(replacement, ignore_errors=True)
        components.mkdir(exist_ok=True)
        shutil.copytree(staged, replacement)
        shutil.rmtree(target, ignore_errors=True)
        replacement.rename(target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", type=Path, default=Path("/config"))
    args = parser.parse_args()
    initialize_config(args.config_dir)
    manifest = args.config_dir / "custom_components" / "auth_oidc" / "manifest.json"
    if manifest.exists() and json.loads(manifest.read_text()).get("version") == VERSION:
        return
    with urllib.request.urlopen(URL, timeout=60) as response:
        install(args.config_dir, response.read())


if __name__ == "__main__":
    main()
