"""Opt-in registry blob and repository-layer retention for Props E2E tests."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

from docker import DockerClient, from_env
from docker.errors import APIError, NotFound
from docker.models.containers import Container
from docker.models.volumes import Volume
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field
from testcontainers.core.container import DockerContainer

logger = logging.getLogger(__name__)

_OWNER_LABEL = "ducktape.props.registry-cache.owner"
_RECIPE_LABEL = "ducktape.props.registry-cache.recipe"
_NAMESPACE_LABEL = "ducktape.props.registry-cache.namespace"
_OWNER = "props.testing.fixtures.registry_blob_cache.v1"
_REGISTRY_ROOT = "/var/lib/registry"
_NAMESPACE_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")


class RegistryBlobCacheOwnershipError(RuntimeError):
    """A deterministic cache volume exists without the expected ownership labels."""


class RegistryBlobCacheRecipe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    key: str = Field(min_length=1)
    max_bytes: int = Field(ge=1024)


@dataclass(frozen=True)
class RegistryBlobCacheReceipt:
    namespace: str
    recipe: str
    volume_name: str
    container_id: str


def _recipe(image_ref: str, recipe: RegistryBlobCacheRecipe) -> str:
    encoded = json.dumps(
        {"image_ref": image_ref, "recipe": recipe.model_dump()}, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _registry_blob_cache_lock() -> FileLock:
    directory = Path(f"/var/tmp/ducktape-props-registry-cache-{os.getuid()}")
    directory.mkdir(mode=0o700, exist_ok=True)
    directory_stat = directory.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise PermissionError(f"Props registry cache lease directory has unexpected owner or mode: {directory}")

    lock_path = directory / "lease.lock"
    with suppress(FileNotFoundError):
        lock_stat = lock_path.lstat()
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_uid != os.getuid()
            or stat.S_IMODE(lock_stat.st_mode) != 0o600
        ):
            raise PermissionError(f"Props registry cache lease file has unexpected owner or mode: {lock_path}")
    return FileLock(lock_path, timeout=0, mode=0o600, fallback_to_soft=False)


def _ensure_volume(docker_client: DockerClient, *, namespace: str, recipe: str) -> str:
    labels = {_OWNER_LABEL: _OWNER, _RECIPE_LABEL: recipe, _NAMESPACE_LABEL: namespace}
    name = f"props-registry-{namespace}-{recipe[:16]}-data"
    volume: Volume
    try:
        volume = docker_client.volumes.get(name)
    except NotFound:
        volume = docker_client.volumes.create(name=name, labels=labels)
    if any((volume.attrs.get("Labels") or {}).get(key) != value for key, value in labels.items()):
        raise RegistryBlobCacheOwnershipError(f"registry cache volume {name!r} has unexpected ownership labels")
    return name


def _prepare_registry_cache(docker_client: DockerClient, *, image_ref: str, volume_name: str, max_bytes: int) -> None:
    # Check at release too: ExitStack runs cleanup even if stopping the registry
    # failed. A dead fixture can also leave a container after releasing flock.
    consumers = docker_client.containers.list(all=True, filters={"volume": volume_name})
    if consumers:
        raise RegistryBlobCacheOwnershipError(
            f"registry cache volume {volume_name!r} is already attached to containers "
            f"{[container.short_id for container in consumers]}"
        )
    # The HTTP API cannot enumerate untagged manifests or abandoned uploads.
    # Scrub all repositories while the registry is stopped, then bound the bytes
    # left for recycling. GC would discard our deliberately unreferenced blobs.
    script = dedent(
        r"""
        root="$1"
        max_kib="$2"
        repositories="$root/docker/registry/v2/repositories"
        if [ -d "$repositories" ]; then
            find "$repositories" -type d \( -name _manifests -o -name _uploads \) \
                -prune -exec rm -rf '{}' +
        fi
        used_kib="$(du -sk "$root")"
        used_kib="${used_kib%%[!0-9]*}"
        if [ "$used_kib" -gt "$max_kib" ]; then
            find "$root" -mindepth 1 -maxdepth 1 -exec rm -rf '{}' +
        fi
        printf '%s\n' "$used_kib"
        """
    )
    reset: Container = docker_client.containers.create(
        image=image_ref,
        entrypoint=["/bin/sh", "-ec"],
        command=[script, "registry-cache-reset", _REGISTRY_ROOT, str(max_bytes // 1024)],
        volumes={volume_name: {"bind": _REGISTRY_ROOT, "mode": "rw"}},
        network_mode="none",
        detach=True,
    )
    with ExitStack() as cleanup:
        cleanup.callback(reset.remove, force=True)
        reset.start()
        status = reset.wait(timeout=10)
        if status.get("StatusCode") != 0:
            output = reset.logs().decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"registry cache preparation exited {status.get('StatusCode')}: {output}")
        previous_bytes = int(reset.logs(stdout=True, stderr=False).decode().strip()) * 1024
        if previous_bytes > max_bytes:
            logger.info("Discarded oversized owned registry cache %s (%d bytes)", volume_name, previous_bytes)


@contextmanager
def cached_registry_container(
    registry: DockerContainer, *, image_ref: str, recipe: RegistryBlobCacheRecipe, namespace: str
) -> Iterator[RegistryBlobCacheReceipt]:
    """Start a fresh registry with only its CAS blobs and repo layer links retained."""
    if not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError(f"invalid registry cache namespace: {namespace!r}")
    if not image_ref:
        raise ValueError("image_ref must be nonempty")
    recipe_digest = _recipe(image_ref, recipe)
    with ExitStack() as stack:
        stack.enter_context(_registry_blob_cache_lock())
        docker_client = stack.enter_context(closing(from_env()))
        volume_name = _ensure_volume(docker_client, namespace=namespace, recipe=recipe_digest)
        registry.with_env("REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY", _REGISTRY_ROOT)
        # Distribution's filesystem Move uses os.Rename from _uploads into blobs, so both
        # paths must share one mount: https://github.com/distribution/distribution/blob/v3.0.0/registry/storage/driver/filesystem/driver.go
        registry.with_volume_mapping(volume_name, _REGISTRY_ROOT, mode="rw")
        _prepare_registry_cache(docker_client, image_ref=image_ref, volume_name=volume_name, max_bytes=recipe.max_bytes)
        # ExitStack is LIFO: stop the registry before scrubbing metadata for the next attach.
        stack.callback(
            _prepare_registry_cache,
            docker_client,
            image_ref=image_ref,
            volume_name=volume_name,
            max_bytes=recipe.max_bytes,
        )
        stack.callback(registry.stop)
        registry.start()
        yield RegistryBlobCacheReceipt(
            namespace=namespace, recipe=recipe_digest, volume_name=volume_name, container_id=registry.get_container_id()
        )


def registry_blob_cache_usage_bytes(registry: DockerContainer, receipt: RegistryBlobCacheReceipt) -> int | None:
    """Read the cache volume's size from one Docker daemon usage snapshot."""
    try:
        usage = registry.get_docker_client().client.df().get("Volumes")
    except APIError, NotImplementedError:
        logger.warning("Docker volume usage is unavailable for the Props registry blob cache", exc_info=True)
        return None

    if usage is None:
        return None
    for volume in usage:
        if volume["Name"] == receipt.volume_name:
            size = (volume.get("UsageData") or {}).get("Size")
            return size if isinstance(size, int) and size >= 0 else None
    return None
