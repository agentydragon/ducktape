import gzip
import hashlib
import io
import tarfile
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_bazel

from util.kubernetes import CustomObjectsClient
from x.agentplane.indexing.source import ArchiveLimits, FluxSource, SourceNotReadyError, read_archive


def archive_bytes(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, contents, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(contents)
            if kind == tarfile.SYMTYPE:
                member.linkname = "../../outside"
            archive.addfile(member, io.BytesIO(contents))
    return output.getvalue()


@pytest.fixture
def archive() -> bytes:
    return archive_bytes(
        [
            ("./src/", b"", tarfile.DIRTYPE),
            ("./src/example.py", b"print('test')\n", tarfile.REGTYPE),
            ("empty.txt", b"", tarfile.REGTYPE),
            ("symlink", b"", tarfile.SYMTYPE),
        ]
    )


@pytest.fixture
def custom_objects(archive: bytes) -> AsyncMock:
    api = AsyncMock(spec=CustomObjectsClient)
    api.get_namespaced_custom_object.return_value = {
        "metadata": {"generation": 2},
        "spec": {"url": "https://git.test/example.git"},
        "status": {
            "observedGeneration": 2,
            "conditions": [{"type": "Ready", "status": "True"}],
            "artifact": {
                "url": "http://source.test/snapshot.tar.gz",
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                "revision": "test@sha1:0123456789",
            },
        },
    }
    return api


def test_read_archive(archive: bytes) -> None:
    assert read_archive(archive, ArchiveLimits()) == {"src/example.py": b"print('test')\n", "empty.txt": b""}


@pytest.mark.parametrize("path", ["../escape", "/absolute", "nested/../../escape", "bad\\path", "."])
def test_reject_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError, match="archive path"):
        read_archive(archive_bytes([(path, b"contents", tarfile.REGTYPE)]), ArchiveLimits())


def test_reject_duplicate_normalized_paths() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        read_archive(
            archive_bytes([("./same", b"one", tarfile.REGTYPE), ("same", b"two", tarfile.REGTYPE)]), ArchiveLimits()
        )


@pytest.mark.parametrize("kind", [tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE])
def test_reject_special_entries(kind: bytes) -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        read_archive(archive_bytes([("special", b"", kind)]), ArchiveLimits())


@pytest.mark.parametrize(
    "limits",
    [
        ArchiveLimits(compressed_bytes=1),
        ArchiveLimits(expanded_bytes=1024),
        ArchiveLimits(file_bytes=1),
        ArchiveLimits(entries=1),
    ],
)
def test_archive_limits(archive: bytes, limits: ArchiveLimits) -> None:
    with pytest.raises(ValueError, match="limit"):
        read_archive(archive, limits)


def test_bounds_decompression_before_processing_tar_headers() -> None:
    with pytest.raises(ValueError, match="expanded byte limit"):
        read_archive(gzip.compress(b"\0" * 100_000), ArchiveLimits(expanded_bytes=1024))


@pytest.fixture
def source_factory(custom_objects: AsyncMock) -> Callable[[httpx.AsyncClient, ArchiveLimits], FluxSource]:
    def create(http: httpx.AsyncClient, limits: ArchiveLimits) -> FluxSource:
        return FluxSource(
            custom_objects=cast(CustomObjectsClient, custom_objects),
            http=http,
            namespace="test-flux",
            name="test-repository",
            limits=limits,
        )

    return create


async def test_fetch_snapshot_and_skip_unchanged(
    archive: bytes, custom_objects: AsyncMock, source_factory: Callable[[httpx.AsyncClient, ArchiveLimits], FluxSource]
) -> None:
    requests: list[httpx.Request] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=httpx.ByteStream(archive))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as http:
        source = source_factory(http, ArchiveLimits())
        snapshot = await source.snapshot(current_digest=None)
        assert snapshot is not None
        assert snapshot.files == {"src/example.py": b"print('test')\n", "empty.txt": b""}
        assert snapshot.revision == "test@sha1:0123456789"
        assert snapshot.repository_url == "https://git.test/example.git"
        assert (
            await source.snapshot(
                current_digest=snapshot.digest,
                current_revision=snapshot.revision,
                current_repository_url=snapshot.repository_url,
            )
            is None
        )
        assert len(requests) == 1
        custom_objects.get_namespaced_custom_object.return_value["status"]["artifact"]["revision"] = (
            "test@sha1:9876543210"
        )
        next_snapshot = await source.snapshot(
            current_digest=snapshot.digest,
            current_revision=snapshot.revision,
            current_repository_url=snapshot.repository_url,
        )
        assert next_snapshot is not None
        assert next_snapshot.revision == "test@sha1:9876543210"
        assert next_snapshot.files == snapshot.files
        custom_objects.get_namespaced_custom_object.return_value["spec"]["url"] = "https://git.test/moved.git"
        relocated = await source.snapshot(
            current_digest=next_snapshot.digest,
            current_revision=next_snapshot.revision,
            current_repository_url=next_snapshot.repository_url,
        )
        assert relocated is not None
        assert relocated.repository_url == "https://git.test/moved.git"
        custom_objects.get_namespaced_custom_object.assert_awaited_with(
            group="source.toolkit.fluxcd.io",
            version="v1",
            namespace="test-flux",
            plural="gitrepositories",
            name="test-repository",
        )


@pytest.mark.parametrize("problem", ["missing", "failed", "stale"])
async def test_unready_source_does_not_download(
    custom_objects: AsyncMock, source_factory: Callable[[httpx.AsyncClient, ArchiveLimits], FluxSource], problem: str
) -> None:
    resource = custom_objects.get_namespaced_custom_object.return_value
    match problem:
        case "missing":
            resource["status"].pop("artifact")
        case "failed":
            resource["status"]["conditions"] = [{"type": "Ready", "status": "False", "message": "fetch failed"}]
        case "stale":
            resource["metadata"]["generation"] = 3
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected download"))
    ) as http:
        with pytest.raises(SourceNotReadyError):
            await source_factory(http, ArchiveLimits()).snapshot(current_digest=None)


async def test_reject_digest_mismatch(source_factory: Callable[[httpx.AsyncClient, ArchiveLimits], FluxSource]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(b"wrong")))
    ) as http:
        with pytest.raises(ValueError, match="digest mismatch"):
            await source_factory(http, ArchiveLimits()).snapshot(current_digest=None)


async def test_bound_download(
    archive: bytes, source_factory: Callable[[httpx.AsyncClient, ArchiveLimits], FluxSource]
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(archive)))
    ) as http:
        with pytest.raises(ValueError, match="compressed byte limit"):
            await source_factory(http, ArchiveLimits(compressed_bytes=1)).snapshot(current_digest=None)


async def test_reject_http_content_encoding(
    source_factory: Callable[[httpx.AsyncClient, ArchiveLimits], FluxSource],
) -> None:
    def encoded_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"unread encoded body")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(encoded_response)) as http:
        with pytest.raises(ValueError, match="identity content encoding"):
            await source_factory(http, ArchiveLimits()).snapshot(current_digest=None)


if __name__ == "__main__":
    pytest_bazel.main()
