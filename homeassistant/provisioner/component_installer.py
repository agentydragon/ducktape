"""Shared installer for checksum-pinned Home Assistant components."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

import aiohttp
from settings import ComponentConfig


def _safe_extract(payload: bytes, destination: Path) -> None:
    """Extract an archive while rejecting members outside its destination."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = destination.resolve()
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if root not in target.parents and target != root:
                raise ValueError(f"unsafe archive member: {member.filename}")
        archive.extractall(destination)


def _component_source(extracted: Path, archive_path: str) -> Path:
    """Resolve the configured component directory in an extracted archive."""
    candidates = [extracted] if archive_path == "." else sorted(extracted.glob(archive_path))
    if len(candidates) != 1 or not candidates[0].is_dir():
        raise ValueError(f"expected one component at {archive_path!r}, found {len(candidates)}")
    return candidates[0]


def _initialize_component_config(config_dir: Path, component: ComponentConfig) -> None:
    """Create a component's mutable YAML files without overwriting them."""
    root = config_dir.resolve()
    for name in component.config_files:
        path = (config_dir / name).resolve()
        if root not in path.parents:
            raise ValueError(f"unsafe component config file: {name}")
        if not path.exists():
            path.write_text("[]\n")


async def initialize_component_config(config_dir: Path, component: ComponentConfig) -> None:
    """Create a component's mutable YAML files without overwriting them."""
    await asyncio.to_thread(_initialize_component_config, config_dir, component)


def _install_component(config_dir: Path, payload: bytes, component: ComponentConfig) -> None:
    """Validate and atomically replace a component in the HA config."""
    digest = hashlib.sha256(payload).hexdigest()
    if digest != component.sha256:
        raise ValueError(f"{component.install_dir} archive SHA256 mismatch: got {digest}, want {component.sha256}")

    components = config_dir / "custom_components"
    target = components / component.install_dir
    with tempfile.TemporaryDirectory(dir=config_dir) as temporary:
        extracted = Path(temporary) / "source"
        extracted.mkdir()
        _safe_extract(payload, extracted)
        source = _component_source(extracted, component.archive_path)
        manifest = json.loads((source / "manifest.json").read_text())
        if manifest.get("version") != component.version:
            raise ValueError(f"{component.install_dir} manifest has unexpected version: {manifest.get('version')}")
        if component.manifest_domain is not None and manifest.get("domain") != component.manifest_domain:
            raise ValueError(f"{component.install_dir} manifest has unexpected domain: {manifest.get('domain')}")

        replacement = components / f".{component.install_dir}.new"
        shutil.rmtree(replacement, ignore_errors=True)
        components.mkdir(exist_ok=True)
        shutil.copytree(source, replacement)
        shutil.rmtree(target, ignore_errors=True)
        replacement.rename(target)


async def install_component(config_dir: Path, payload: bytes, component: ComponentConfig) -> None:
    """Validate and atomically replace a component in the HA config."""
    await asyncio.to_thread(_install_component, config_dir, payload, component)


def _installed_version(manifest: Path) -> str | None:
    version = json.loads(manifest.read_text()).get("version")
    return version if isinstance(version, str) else None


async def install_component_from_url(
    session: aiohttp.ClientSession, config_dir: Path, component: ComponentConfig
) -> None:
    """Install one configured component unless its requested version is present."""
    await initialize_component_config(config_dir, component)
    manifest = config_dir / "custom_components" / component.install_dir / "manifest.json"
    if manifest.exists() and await asyncio.to_thread(_installed_version, manifest) == component.version:
        return
    async with session.get(component.url, timeout=aiohttp.ClientTimeout(total=60)) as response:
        response.raise_for_status()
        payload = await response.read()
    await install_component(config_dir, payload, component)


async def install_components(
    session: aiohttp.ClientSession, config_dir: Path, components: Iterable[ComponentConfig]
) -> None:
    """Install all configured custom components."""
    for component in components:
        await install_component_from_url(session, config_dir, component)
