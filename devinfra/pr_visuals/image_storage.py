"""CAS image staging and the S3 image publication barrier for visual reviews."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError
from PIL import Image, UnidentifiedImageError

from devinfra.pr_visuals.artifacts import Runner
from devinfra.pr_visuals.render_common import shared_image_key

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

logger = logging.getLogger(__name__)
S3_IMAGE_UPLOAD_WORKERS = 8


@dataclass(frozen=True)
class ImageSource:
    uri: str
    invocation: str
    size: int | None


def _is_missing_object(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404")


@dataclass(frozen=True)
class Download:
    uri: str
    invocation: str
    destination: Path


def download_all(downloads: list[Download], *, bbapi: Path, run: Runner) -> None:
    """Download a batch of listed CAS blobs in one bbapi operation.

    The manifest carries the URI BuildBuddy advertised and its exact destination, so the
    CLI can download concurrently without resolving the invocation's event stream once
    per file.
    """
    if not downloads:
        return
    for download in downloads:
        download.destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = [
        {"uri": download.uri, "destination": str(download.destination), "invocation_id": download.invocation}
        for download in downloads
    ]
    manifest_dir = downloads[0].destination.parent
    manifest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=manifest_dir, delete=False) as stream:
        json.dump(manifest, stream)
        manifest_path = Path(stream.name)
    try:
        result = run(
            [bbapi, "--timeout", "30s", "blob", "download", "--manifest", str(manifest_path)],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"bbapi blob download failed: {result.stderr.strip()}")
    finally:
        manifest_path.unlink(missing_ok=True)
    missing = [str(download.destination) for download in downloads if not download.destination.is_file()]
    if missing:
        raise RuntimeError(f"bbapi blob download did not create destinations: {', '.join(missing)}")


def _check_shared_image(s3: S3Client, *, bucket: str, digest: str, expected_size: int | None) -> bool:
    image_key = shared_image_key(digest)
    try:
        response = s3.head_object(Bucket=bucket, Key=image_key)
    except ClientError as error:
        if _is_missing_object(error):
            return False
        raise
    # HEAD establishes presence and size, not content integrity or PNG validity.
    # Reuse trusts the publisher's write-limited, immutable image pool.
    return expected_size is None or response["ContentLength"] == expected_size


def _put_shared_image(
    s3: S3Client, *, staged_image_root: Path, bucket: str, digest: str, cache_control: str, expected_size: int | None
) -> None:
    image_key = shared_image_key(digest)
    image_path = staged_image_root / image_key
    if not image_path.is_file():
        raise FileNotFoundError(f"missing local screenshot for absent published image {image_key}")
    try:
        s3.put_object(
            Bucket=bucket,
            Key=image_key,
            Body=image_path.read_bytes(),
            ContentType="image/png",
            CacheControl=cache_control,
            IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "PreconditionFailed":
            raise
        response = s3.head_object(Bucket=bucket, Key=image_key)
        if expected_size is not None and response["ContentLength"] != expected_size:
            raise ValueError(f"published image {image_key} has the wrong size") from error


def _validate_candidate_image(path: Path, *, digest: str, size: int | None) -> None:
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != digest or (size is not None and len(body) != size):
        raise ValueError(f"screenshot content does not match BuildBuddy CAS digest {digest}")
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise ValueError(f"screenshot {digest} is not PNG")
            image.verify()
    except UnidentifiedImageError as error:
        raise ValueError(f"screenshot {digest} is not PNG") from error


def publish_shared_images(
    s3: S3Client,
    *,
    bucket: str,
    staged_image_root: Path,
    digests: set[str],
    sources: Mapping[str, ImageSource],
    bbapi: Path,
    run: Runner,
    cache_control: str,
) -> None:
    """Finish every shared image before the caller publishes review metadata."""
    expected_sizes = {digest: sources[digest].size if digest in sources else None for digest in digests}
    for digest in digests:
        path = staged_image_root / shared_image_key(digest)
        if expected_sizes[digest] is None and path.is_file():
            expected_sizes[digest] = path.stat().st_size
    failures: list[Exception] = []
    missing: set[str] = set()
    logger.info("Checking and uploading %d shared images", len(digests))
    with ThreadPoolExecutor(max_workers=S3_IMAGE_UPLOAD_WORKERS) as executor:
        checks = {
            executor.submit(
                _check_shared_image, s3, bucket=bucket, digest=digest, expected_size=expected_sizes[digest]
            ): digest
            for digest in digests
        }
        for future in as_completed(checks):
            try:
                if not future.result():
                    missing.add(checks[future])
            except Exception as error:
                failures.append(error)
        if failures:
            raise failures[0]

        downloads: list[Download] = []
        for digest in missing:
            path = staged_image_root / shared_image_key(digest)
            if path.is_file():
                continue
            if digest not in sources:
                raise FileNotFoundError(
                    f"missing local screenshot for absent published image {shared_image_key(digest)}"
                )
            source = sources[digest]
            downloads.append(Download(source.uri, source.invocation, path))
        download_all(downloads, bbapi=bbapi, run=run)
        if sources:
            for digest in missing:
                _validate_candidate_image(
                    staged_image_root / shared_image_key(digest), digest=digest, size=expected_sizes[digest]
                )

        uploads = [
            executor.submit(
                _put_shared_image,
                s3,
                staged_image_root=staged_image_root,
                bucket=bucket,
                digest=digest,
                cache_control=cache_control,
                expected_size=expected_sizes[digest],
            )
            for digest in missing
        ]
        # Drain every upload before deciding whether metadata may publish.
        for upload_future in as_completed(uploads):
            try:
                upload_future.result()
            except Exception as error:
                failures.append(error)
    if failures:
        raise failures[0]
