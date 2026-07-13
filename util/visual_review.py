"""Shared schema for Bazel visual-review manifests."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SCHEMA: Literal["ducktape.visual-review.v1"] = "ducktape.visual-review.v1"
MANIFEST_NAME = "visual-review.json"


class VisualReviewAsset(BaseModel):
    path: str
    label: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if Path(value).name != value or not value.endswith(".png"):
            raise ValueError("visual-review assets must be safe PNG basenames")
        return value


class VisualReviewManifest(BaseModel):
    schema_: Literal["ducktape.visual-review.v1"] = Field(default=SCHEMA, alias="schema")
    title: str
    assets: list[VisualReviewAsset]

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("visual-review title must not be empty")
        return value

    @field_validator("assets")
    @classmethod
    def validate_assets(cls, value: list[VisualReviewAsset]) -> list[VisualReviewAsset]:
        if not value:
            raise ValueError("visual review must contain at least one asset")
        paths = [asset.path for asset in value]
        if len(paths) != len(set(paths)):
            raise ValueError("visual-review asset paths must be unique")
        return value
