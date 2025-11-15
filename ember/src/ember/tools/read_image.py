from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..object_store import ImageHandle, ObjectStoreClient
from ..tool_execution import ToolSpec

logger = logging.getLogger(__name__)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ReadImageArgs(BaseModel):
    path: Path = Field(..., description="Workspace-relative path to the image to upload.")
    model_config = ConfigDict(extra="forbid")


class ImageUploadError(BaseModel):
    error: str = Field(..., description="Reason the image could not be uploaded.")
    model_config = ConfigDict(extra="forbid")


def build_spec(workspace_root: Path, client: ObjectStoreClient) -> ToolSpec:
    async def handler(args: ReadImageArgs) -> ImageHandle | ImageUploadError:
        try:
            file_path = _resolve_path(workspace_root, args.path)
            mime_type = _guess_mime_type(file_path)
            _enforce_limits(file_path)
            logger.info("Uploading image %s (%s)", file_path, mime_type)
            return client.upload_image(file_path, mime_type)
        except ValueError as exc:
            logger.warning("read_image validation failed: %s", exc)
            return ImageUploadError(error=str(exc))
        except Exception as exc:  # pragma: no cover - network errors are environmental
            logger.exception("read_image failed")
            return ImageUploadError(error=f"failed to upload image: {exc}")

    return ToolSpec(
        name="read_image",
        description=(
            "Upload an image from the workspace to the Ember object store and return "
            "a signed URL for model consumption."
        ),
        handler=handler,
    )


def _resolve_path(workspace_root: Path, candidate: Path) -> Path:
    base = workspace_root.resolve()
    path = (base / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not path.exists():
        raise ValueError(f"Image path does not exist: {candidate}")
    if not path.is_file():
        raise ValueError(f"Image path is not a file: {candidate}")
    if not path.is_relative_to(base):
        raise ValueError(f"Image path {candidate} is outside the workspace")
    return path


def _guess_mime_type(file_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(file_path.name)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"Unsupported image type for {file_path.name}")
    return mime_type


def _enforce_limits(file_path: Path) -> None:
    size = file_path.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(f"Image {file_path.name} is {size} bytes; limit is {_MAX_IMAGE_BYTES} bytes")
