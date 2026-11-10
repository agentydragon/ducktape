from __future__ import annotations

import hashlib
import io
import json
import zlib

import pytest
import pytest_bazel
from botocore.exceptions import ClientError

from devinfra.pr_visuals.record_cache import MAX_DECOMPRESSED_BYTES, RecordCache
from devinfra.testing.output_records import SCHEMA, OutputRecord


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.read_error: str | None = None
        self.write_error: str | None = None

    def get_object(self, **kwargs: str) -> dict[str, io.BytesIO]:
        if self.read_error:
            raise ClientError({"Error": {"Code": self.read_error}}, "GetObject")
        try:
            return {"Body": io.BytesIO(self.objects[kwargs["Bucket"], kwargs["Key"]])}
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject") from None

    def put_object(self, **kwargs: object) -> None:
        if self.write_error:
            raise ClientError({"Error": {"Code": self.write_error}}, "PutObject")
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[str(kwargs["Bucket"]), str(kwargs["Key"])] = body


def _record(case: str) -> bytes:
    return (
        OutputRecord(schema_=SCHEMA, case_id=case, checkpoint=None, role="screenshot", path="files/scene.png")
        .model_dump_json(by_alias=True)
        .encode()
    )


def _remember(cache: RecordCache, case: str) -> tuple[str, int]:
    body = _record(case)
    key = hashlib.sha256(body).hexdigest(), len(body)
    cache.remember(*key, body)
    return key


def test_snapshot_reused_across_fresh_clients_and_devel_updates_union() -> None:
    s3 = FakeS3()
    first = RecordCache(s3, "private-records", "owner/repo", writable=True)
    first.load()
    a = _remember(first, "a")
    first.save()

    second = RecordCache(s3, "private-records", "owner/repo", writable=True)
    second.load()
    b = _remember(second, "b")
    second.save()

    pr = RecordCache(s3, "private-records", "owner/repo")
    pr.load()
    assert pr.get(*a) == _record("a")
    assert pr.get(*b) == _record("b")
    _remember(pr, "pr-only")
    pr.save()
    later = RecordCache(s3, "private-records", "owner/repo")
    later.load()
    assert len(later.entries) == 2


def test_overlapping_devel_writers_can_only_lose_cache_hits() -> None:
    s3 = FakeS3()
    first = RecordCache(s3, "private-records", "owner/repo", writable=True)
    second = RecordCache(s3, "private-records", "owner/repo", writable=True)
    first.load()
    second.load()
    a = _remember(first, "a")
    b = _remember(second, "b")
    first.save()
    second.save()

    reader = RecordCache(s3, "private-records", "owner/repo")
    reader.load()
    assert reader.get(*b) == _record("b")
    assert reader.get(*a) is None


def test_corrupt_entries_are_ignored_and_oversized_snapshot_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    s3 = FakeS3()
    cache = RecordCache(s3, "private-records", "owner/repo")
    body = _record("valid")
    digest = hashlib.sha256(body).hexdigest()
    s3.objects[cache.bucket, cache.key] = zlib.compress(
        json.dumps({"version": 1, "entries": [[digest, len(body), "Y29ycnVwdA=="]]}).encode()
    )
    cache.load()
    assert cache.get(digest, len(body)) is None
    assert "Ignored 1 invalid entries" in caplog.text

    s3.objects[cache.bucket, cache.key] = zlib.compress(b"x" * (MAX_DECOMPRESSED_BYTES + 1))
    cache.load()
    assert "oversized or truncated" in caplog.text


@pytest.mark.parametrize("error", ["NoSuchBucket", "AccessDenied"])
def test_unavailable_cache_falls_back_and_does_not_write(error: str, caplog: pytest.LogCaptureFixture) -> None:
    s3 = FakeS3()
    s3.read_error = error
    cache = RecordCache(s3, "private-records", "owner/repo", writable=True)
    cache.load()
    _remember(cache, "new")
    cache.save()
    assert not s3.objects
    assert "using BuildBuddy" in caplog.text


def test_failed_write_is_logged_and_does_not_affect_record_bytes(caplog: pytest.LogCaptureFixture) -> None:
    s3 = FakeS3()
    s3.write_error = "AccessDenied"
    cache = RecordCache(s3, "private-records", "owner/repo", writable=True)
    cache.load()
    key = _remember(cache, "new")
    cache.save()
    assert cache.get(*key) == _record("new")
    assert "publication continues" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
