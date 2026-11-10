"""Bounded, private S3 snapshot of CAS-addressed test output record bodies.

The snapshot is an optimization only. Callers obtain result identities and artifact
references from a fresh BuildBuddy listing, then verify each body against that listing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import zlib
from contextlib import closing
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from devinfra.testing.output_records import OutputRecord

logger = logging.getLogger(__name__)

MAX_COMPRESSED_BYTES = 8 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 24 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024
MAX_RECORDS = 20_000
MAX_BODY_BYTES = 16 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RecordCache:
    """One repository's snapshot; concurrent writers may lose hits, never correctness."""

    def __init__(self, client: Any, bucket: str, repository: str, *, writable: bool = False) -> None:
        self.client = client
        self.bucket = bucket
        self.key = f"record-cache/v1/{hashlib.sha256(repository.encode()).hexdigest()}.json.zlib"
        self.writable = writable
        self.entries: dict[tuple[str, int], bytes] = {}
        self.available = True
        self.dirty = False

    def load(self) -> None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key)
            with closing(response["Body"]) as body:
                compressed = body.read(MAX_COMPRESSED_BYTES + 1)
            if len(compressed) > MAX_COMPRESSED_BYTES:
                raise ValueError("compressed snapshot exceeds size limit")
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(compressed, MAX_DECOMPRESSED_BYTES + 1)
            if len(raw) > MAX_DECOMPRESSED_BYTES or not decompressor.eof or decompressor.unused_data:
                raise ValueError("snapshot is oversized or truncated")
            snapshot = json.loads(raw)
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("version") != 1
                or not isinstance(snapshot.get("entries"), list)
            ):
                raise ValueError("unsupported snapshot format")
            if len(snapshot["entries"]) > MAX_RECORDS:
                raise ValueError("snapshot has too many records")
            rejected = 0
            for entry in snapshot["entries"]:
                try:
                    digest, size, encoded = entry
                    if (
                        not isinstance(digest, str)
                        or _DIGEST.fullmatch(digest) is None
                        or type(size) is not int
                        or size < 0
                        or size > MAX_RECORD_BYTES
                        or not isinstance(encoded, str)
                    ):
                        raise ValueError("invalid record metadata")
                    contents = base64.b64decode(encoded, validate=True)
                    if len(contents) != size or hashlib.sha256(contents).hexdigest() != digest:
                        raise ValueError("record digest or size mismatch")
                    self.entries[digest, size] = contents
                except TypeError, ValueError, binascii.Error:
                    rejected += 1
            if rejected:
                logger.warning("Ignored %d invalid entries in private record cache snapshot", rejected)
            logger.info("Loaded %d verified output records from private cache", len(self.entries))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in ("NoSuchKey", "404"):
                logger.warning("Private record cache read failed; using BuildBuddy: %s", error)
                self.available = False
        except (BotoCoreError, OSError, ValueError, zlib.error) as error:
            logger.warning("Private record cache is invalid; using BuildBuddy: %s", error)

    def get(self, digest: str, size: int) -> bytes | None:
        contents = self.entries.get((digest, size))
        if contents is None:
            return None
        # Recheck against the newly advertised CAS identity, even if load checked it.
        if len(contents) != size or hashlib.sha256(contents).hexdigest() != digest:
            logger.warning("Private record cache entry changed after load; using BuildBuddy")
            self.entries.pop((digest, size))
            return None
        return contents

    def remember(self, digest: str, size: int, contents: bytes) -> None:
        if not self.writable or not self.available or size > MAX_RECORD_BYTES:
            return
        if len(contents) != size or hashlib.sha256(contents).hexdigest() != digest:
            return
        try:
            OutputRecord.model_validate_json(contents)
        except ValueError:
            return
        key = digest, size
        if key in self.entries:
            # Keep bodies used by this complete devel collection ahead of stale entries
            # when an update later has to evict under the snapshot bound.
            self.entries[key] = self.entries.pop(key)
        else:
            self.entries[key] = contents
            self.dirty = True

    def save(self) -> None:
        if not self.dirty or not self.available or not self.writable:
            return
        # Reserve room for base64, JSON punctuation and metadata. The conservative
        # estimate lets us evict in one pass and serialize only once.
        body_bytes = sum(map(len, self.entries.values()))
        encoded_bytes = sum(4 * ((len(body) + 2) // 3) + 128 for body in self.entries.values())
        while (
            len(self.entries) > MAX_RECORDS
            or body_bytes > MAX_BODY_BYTES
            or encoded_bytes > MAX_DECOMPRESSED_BYTES - 4096
        ):
            oldest_key = next(iter(self.entries))
            oldest = self.entries.pop(oldest_key)
            body_bytes -= len(oldest)
            encoded_bytes -= 4 * ((len(oldest) + 2) // 3) + 128
        serialized = json.dumps(
            {
                "version": 1,
                "entries": [
                    [digest, size, base64.b64encode(body).decode()] for (digest, size), body in self.entries.items()
                ],
            },
            separators=(",", ":"),
        ).encode()
        if len(serialized) > MAX_DECOMPRESSED_BYTES:
            logger.warning("Private record cache snapshot exceeds expanded size limit; skipping write")
            return
        compressed = zlib.compress(serialized)
        if len(compressed) > MAX_COMPRESSED_BYTES:
            logger.warning("Private record cache snapshot exceeds compressed size limit; skipping write")
            return
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=compressed,
                ContentType="application/json",
                ContentEncoding="deflate",
            )
            logger.info("Saved %d output records to private cache", len(self.entries))
            self.dirty = False
        except (BotoCoreError, ClientError, OSError) as error:
            logger.warning("Private record cache write failed; publication continues: %s", error)
