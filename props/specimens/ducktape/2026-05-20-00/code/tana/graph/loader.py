from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tana.graph.graph import TanaGraph


class WorkspaceDoc(BaseModel):
    id: str
    props: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class WorkspaceExport(BaseModel):
    format_version: int | None = Field(1, alias="formatVersion")
    docs: list[WorkspaceDoc]
    model_config = ConfigDict(extra="allow")


def load_workspace(path: Path) -> TanaGraph:
    payload = WorkspaceExport.model_validate_json(path.read_text(encoding="utf-8"))
    documents = [doc.model_dump(mode="python") for doc in payload.docs]
    return TanaGraph.from_documents(documents)
