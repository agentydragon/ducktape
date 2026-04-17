"""Result types for the Grocy MCP eval."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class EvalResult(BaseModel):
    model: str
    api: str
    postmortem_text: str
    transcript_path: Path
