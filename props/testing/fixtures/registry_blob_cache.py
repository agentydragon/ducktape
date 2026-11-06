"""Opt-in registry blob and repository-layer retention for Props E2E tests."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from docker import DockerClient, from_env
from docker.errors import APIError
from docker.models.containers import Container
from docker.models.volumes import Volume
from testcontainers.core.container import DockerContainer

logger = logging.getLogger(__name__)

_OWNER_LABEL = "ducktape.props.registry-cache.owner"
_RECIPE_LABEL = "ducktape.props.registry-cache.recipe"
_NAMESPACE_LABEL = "ducktape.props.registry-cache.namespace"
_OWNER = "props.testing.fixtures.registry_blob_cache.v1"
_LAYOUT_VERSION = "registry-v2-single-volume-repo-metadata-reset-v2"
_REGISTRY_ROOT = "/var/lib/registry"
_REGISTRY_STORAGE_ROOT = "/var/lib/registry/docker/registry/v2"
_NAMESPACE_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")


class RegistryBlobCacheLeaseBusyError(RuntimeError):
    """Another Props registry cache lifecycle is active on this Docker daemon."""


class RegistryBlobCacheOwnershipError(RuntimeError):
    """A deterministic cache volume exists without the expected ownership labels."""


@dataclass(frozen=True)
class RegistryBlobCacheReceipt:
    namespace: str
    recipe: str
    volume_names: tuple[str, ...]
    container_id: str


def _recipe(image_ref: str, repositories: Sequence[str]) -> str:
    recipe = json.dumps(
        {"image_ref": image_ref, "layout": _LAYOUT_VERSION, "repositories": sorted(set(repositories))},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(recipe).hexdigest()


def _volume_name(namespace: str, recipe: str) -> str:
    prefix = f"props-registry-{namespace}-{recipe[:16]}"
    return f"{prefix}-data"


@contextmanager
def _registry_blob_cache_lease() -> Iterator[None]:
    directory = Path(f"/var/tmp/ducktape-props-registry-cache-{os.getuid()}")
    with suppress(FileExistsError):
        directory.mkdir(mode=0o700)

    directory_stat = directory.lstat()
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise PermissionError(f"Props registry cache lease directory has unexpected owner or mode: {directory}")

    lock_path = directory / "lease.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "r") as lock_file:
            descriptor = -1
            lock_stat = os.fstat(lock_file.fileno())
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.getuid()
                or stat.S_IMODE(lock_stat.st_mode) != 0o600
            ):
                raise PermissionError(f"Props registry cache lease file has unexpected owner or mode: {lock_path}")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RegistryBlobCacheLeaseBusyError(
                    "another Props registry blob-cache lifecycle holds the lease"
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ensure_volume(docker_client: DockerClient, *, namespace: str, recipe: str) -> str:
    labels = {_OWNER_LABEL: _OWNER, _RECIPE_LABEL: recipe, _NAMESPACE_LABEL: namespace}
    name = _volume_name(namespace, recipe)
    volume: Volume | None = next(
        (candidate for candidate in docker_client.volumes.list(filters={"name": [name]}) if candidate.name == name),
        None,
    )
    if volume is None:
        volume = docker_client.volumes.create(name=name, labels=labels)
    volume.reload()
    if any((volume.attrs.get("Labels") or {}).get(key) != value for key, value in labels.items()):
        raise RegistryBlobCacheOwnershipError(f"registry cache volume {name!r} has unexpected ownership labels")
    return name


def _reset_repository_metadata(
    docker_client: DockerClient, *, image_ref: str, volume_name: str, repositories: Sequence[str]
) -> None:
    script = (
        "for repository do "
        f'rm -rf "{_REGISTRY_STORAGE_ROOT}/repositories/${{repository}}/_manifests" '
        f'"{_REGISTRY_STORAGE_ROOT}/repositories/${{repository}}/_uploads"; '
        "done"
    )
    reset: Container = docker_client.containers.create(
        image=image_ref,
        entrypoint=["/bin/sh", "-ec"],
        command=[script, "registry-cache-reset", *sorted(set(repositories))],
        volumes={volume_name: {"bind": _REGISTRY_ROOT, "mode": "rw"}},
        network_mode="none",
        detach=True,
    )
    try:
        reset.start()
        status = reset.wait(timeout=10)
        if status.get("StatusCode") != 0:
            output = reset.logs().decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(f"registry metadata reset exited {status.get('StatusCode')}: {output}")
    finally:
        reset.remove(force=True)


@contextmanager
def cached_registry_container(
    registry: DockerContainer, *, image_ref: str, repositories: Sequence[str], namespace: str
) -> Iterator[RegistryBlobCacheReceipt]:
    """Start a fresh registry with only its CAS blobs and repo layer links retained."""
    if not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError(f"invalid registry cache namespace: {namespace!r}")
    if not image_ref:
        raise ValueError("image_ref must be nonempty")
    if not repositories or any(not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", repo) for repo in repositories):
        raise ValueError("repositories must contain valid, nonempty OCI repository names")

    recipe = _recipe(image_ref, repositories)
    with ExitStack() as stack:
        stack.enter_context(_registry_blob_cache_lease())
        docker_client: DockerClient = from_env()
        stack.callback(docker_client.close)
        volume_name = _ensure_volume(docker_client, namespace=namespace, recipe=recipe)
        # A dead fixture process releases flock but can leave Docker containers.
        # Never reset storage still attached elsewhere, even to a stopped container.
        consumers = docker_client.containers.list(all=True, filters={"volume": volume_name})
        if consumers:
            raise RegistryBlobCacheOwnershipError(
                f"registry cache volume {volume_name!r} is already attached to containers "
                f"{[container.short_id for container in consumers]}"
            )
        registry.with_env("REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY", _REGISTRY_ROOT)
        # Distribution's filesystem Move uses os.Rename from _uploads into blobs, so both
        # paths must share one mount: https://github.com/distribution/distribution/blob/v3.0.0/registry/storage/driver/filesystem/driver.go
        registry.with_volume_mapping(volume_name, _REGISTRY_ROOT, mode="rw")
        _reset_repository_metadata(
            docker_client, image_ref=image_ref, volume_name=volume_name, repositories=repositories
        )
        # ExitStack is LIFO: stop the registry before scrubbing metadata for the next attach.
        stack.callback(
            _reset_repository_metadata,
            docker_client,
            image_ref=image_ref,
            volume_name=volume_name,
            repositories=repositories,
        )
        stack.callback(registry.stop)
        registry.start()
        yield RegistryBlobCacheReceipt(
            namespace=namespace, recipe=recipe, volume_names=(volume_name,), container_id=registry.get_container_id()
        )


def delete_owned_registry_blob_cache_volumes(registry: DockerContainer, *, namespace: str) -> tuple[str, ...]:
    """Delete only cache volumes carrying this helper's owner and exact namespace labels."""
    if not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError(f"invalid registry cache namespace: {namespace!r}")

    deleted: list[str] = []
    with _registry_blob_cache_lease():
        docker_client = registry.get_docker_client().client
        volumes = docker_client.volumes.list(
            filters={"label": [f"{_OWNER_LABEL}={_OWNER}", f"{_NAMESPACE_LABEL}={namespace}"]}
        )
        for volume in volumes:
            volume.reload()
            labels = volume.attrs.get("Labels") or {}
            if labels.get(_OWNER_LABEL) != _OWNER or labels.get(_NAMESPACE_LABEL) != namespace:
                continue
            if not labels.get(_RECIPE_LABEL):
                continue
            name = volume.name
            volume.remove()
            deleted.append(name)
    return tuple(deleted)


def registry_blob_cache_usage_bytes(registry: DockerContainer, receipt: RegistryBlobCacheReceipt) -> int | None:
    """Read one Docker daemon usage snapshot for the cache volumes in a receipt."""
    try:
        usage = registry.get_docker_client().client.df().get("Volumes")
    except APIError, NotImplementedError:
        logger.warning("Docker volume usage is unavailable for the Props registry blob cache", exc_info=True)
        return None

    if usage is None:
        return None
    sizes = {
        volume["Name"]: (volume.get("UsageData") or {}).get("Size")
        for volume in usage
        if volume.get("Name") in receipt.volume_names
    }
    if set(sizes) != set(receipt.volume_names) or any(not isinstance(size, int) or size < 0 for size in sizes.values()):
        return None
    return sum(size for size in sizes.values() if isinstance(size, int))
