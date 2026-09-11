"""Flux GitRepository artifacts as validated, source-independent file snapshots."""

import asyncio
import gzip
import hashlib
import io
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

import httpx
from pydantic import BaseModel, ConfigDict, Field

from util.kubernetes import CustomObjectsClient


@dataclass(frozen=True)
class Snapshot:
    revision: str
    digest: str
    repository_url: str
    files: Mapping[str, bytes]


class ArchiveLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    compressed_bytes: int = Field(default=128 * 1024 * 1024, gt=0)
    expanded_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    file_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    entries: int = Field(default=100_000, gt=0)


class Artifact(BaseModel):
    url: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision: str


class Condition(BaseModel):
    type: str
    status: str
    message: str = ""


class RepositoryStatus(BaseModel):
    observed_generation: int | None = Field(default=None, alias="observedGeneration")
    artifact: Artifact | None = None
    conditions: list[Condition] = Field(default_factory=list)


class RepositorySpec(BaseModel):
    url: str


class RepositoryMetadata(BaseModel):
    generation: int


class Repository(BaseModel):
    metadata: RepositoryMetadata
    spec: RepositorySpec
    status: RepositoryStatus = Field(default_factory=RepositoryStatus)


class SourceNotReadyError(Exception):
    """Flux has not published a ready artifact for the requested configuration."""


def read_archive(data: bytes, limits: ArchiveLimits) -> Mapping[str, bytes]:
    """Read regular files without extracting paths; bound headers as well as file bodies."""
    if len(data) > limits.compressed_bytes:
        raise ValueError("Artifact exceeds compressed byte limit")
    # Bound decompression before tarfile processes PAX/long-name headers, which can
    # themselves consume substantial memory before a member becomes visible.
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
        expanded = compressed.read(limits.expanded_bytes + 1)
    if len(expanded) > limits.expanded_bytes:
        raise ValueError("Artifact exceeds expanded byte limit")
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for count, member in enumerate(archive, start=1):
            if count > limits.entries:
                raise ValueError("Artifact exceeds entry limit")
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError(f"Unsafe archive path: {member.name!r}")
            if str(path) == "." and member.isdir():
                continue
            if str(path) == "." or str(path) in seen:
                raise ValueError(f"Empty or duplicate archive path: {member.name!r}")
            seen.add(str(path))
            # Git symlinks are not documents and must never be followed.
            if member.isdir() or member.issym():
                continue
            if not member.isfile() or member.issparse():
                raise ValueError(f"Unsupported archive entry: {member.name!r}")
            if member.size < 0 or member.size > limits.file_bytes:
                raise ValueError(f"File exceeds byte limit: {member.name!r}")
            contents = archive.extractfile(member)
            assert contents is not None
            with contents:
                files[str(path)] = contents.read()
    return MappingProxyType(files)


class FluxSource:
    def __init__(
        self,
        *,
        custom_objects: CustomObjectsClient,
        http: httpx.AsyncClient,
        namespace: str,
        name: str,
        limits: ArchiveLimits,
    ) -> None:
        self._custom_objects = custom_objects
        self._http = http
        self._namespace = namespace
        self._name = name
        self._limits = limits

    async def snapshot(
        self,
        *,
        current_digest: str | None,
        current_revision: str | None = None,
        current_repository_url: str | None = None,
    ) -> Snapshot | None:
        resource = Repository.model_validate(
            await self._custom_objects.get_namespaced_custom_object(
                group="source.toolkit.fluxcd.io",
                version="v1",
                namespace=self._namespace,
                plural="gitrepositories",
                name=self._name,
            )
        )
        ready = next((condition for condition in resource.status.conditions if condition.type == "Ready"), None)
        if ready is None or ready.status != "True":
            raise SourceNotReadyError(ready.message if ready else "GitRepository has no Ready condition")
        if resource.status.observed_generation != resource.metadata.generation:
            raise SourceNotReadyError("GitRepository has not reconciled its current configuration")
        artifact = resource.status.artifact
        if artifact is None:
            raise SourceNotReadyError("GitRepository has no published artifact")
        if (
            artifact.digest == current_digest
            and artifact.revision == current_revision
            and resource.spec.url == current_repository_url
        ):
            return None
        data = bytearray()
        async with self._http.stream(
            "GET", artifact.url, headers={"Accept-Encoding": "identity"}, follow_redirects=False
        ) as response:
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("Artifact response must use identity content encoding")
            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > self._limits.compressed_bytes:
                    raise ValueError("Artifact exceeds compressed byte limit")
        if "sha256:" + hashlib.sha256(data).hexdigest() != artifact.digest:
            raise ValueError("Artifact digest mismatch")
        files = await asyncio.to_thread(read_archive, bytes(data), self._limits)
        return Snapshot(
            revision=artifact.revision, digest=artifact.digest, repository_url=resource.spec.url, files=files
        )
